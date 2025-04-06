import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple, Set
from pathlib import Path
import aiohttp
from kademlia.network import Server
from kademlia.utils import digest
import socket
import struct
import random
import time
from collections import defaultdict
import hashlib



class DHTNode:
    def __init__(self, port: int, files_dir: Path):
        self.server = Server()
        self.port = port
        self.files_dir = files_dir
        self.logger = logging.getLogger(f"{self.__class__.__name__}:{port}")
        self._is_running = False
        self.peers = set()
        self.torrents = {}
        self.stun_servers = [
            ('stun.l.google.com', 19302),
            ('stun1.l.google.com', 19302),
            ('stun2.l.google.com', 19302)
        ]
        self.peer_exchange_interval = 300
        self.tracker_announce_interval = 1800

    async def start(self) -> None:
        if self._is_running:
            self.logger.warning("Node is already running")
            return

        try:
            await self.server.listen(self.port)
            self._is_running = True
            self.logger.info(f"DHT node started on port {self.port}")

            asyncio.create_task(self.discover_public_ip())
            asyncio.create_task(self.peer_exchange_loop())
            asyncio.create_task(self.tracker_announce_loop())
        except Exception as e:
            self.logger.error(f"Failed to start DHT node: {e}")
            raise

    async def discover_public_ip(self) -> Optional[str]:
        for stun_server, stun_port in self.stun_servers:
            try:
                public_ip = await self._get_public_ip(stun_server, stun_port)
                if public_ip:
                    self.logger.info(f"Public IP discovered via STUN: {public_ip}")
                    return public_ip
            except Exception as e:
                self.logger.warning(f"STUN request to {stun_server} failed: {e}")
        return None

    async def _get_public_ip(self, stun_host: str, stun_port: int) -> Optional[str]:
        reader, writer = await asyncio.open_connection(stun_host, stun_port)

        try:
            transaction_id = random.randbytes(12)
            request = struct.pack('!HHI12s', 0x0001, 0x0000, 0x2112A442, transaction_id)
            writer.write(request)
            await writer.drain()

            response = await reader.read(1024)
            if len(response) < 20:
                return None

            message_type, message_length, magic_cookie, received_transaction_id = \
                struct.unpack('!HHII', response[:12])

            if message_type != 0x0101 or magic_cookie != 0x2112A442 or \
                    received_transaction_id != transaction_id:
                return None

            offset = 20
            while offset < len(response):
                attr_type, attr_length = struct.unpack('!HH', response[offset:offset + 4])
                if attr_type == 0x0020:  # XOR-MAPPED-ADDRESS
                    addr_data = response[offset + 4:offset + 4 + attr_length]
                    port = struct.unpack('!H', addr_data[2:4])[0] ^ 0x2112
                    ip_bytes = struct.unpack('!I', addr_data[4:8])[0] ^ 0x2112A442
                    ip = socket.inet_ntoa(struct.pack('!I', ip_bytes))
                    return f"{ip}:{port}"
                offset += 4 + attr_length
        finally:
            writer.close()
            await writer.wait_closed()
        return None

    async def peer_exchange_loop(self):
        while self._is_running:
            try:
                await self.exchange_peers_with_known_nodes()
            except Exception as e:
                self.logger.error(f"Peer exchange failed: {e}")
            await asyncio.sleep(self.peer_exchange_interval)

    async def exchange_peers_with_known_nodes(self):
        known_nodes = list(self.server.protocol.router.find_neighbors(self.server.node))
        for node in known_nodes:
            try:
                response = await self.server.protocol.call_get_peers(node)
                if response and 'peers' in response:
                    for peer in response['peers']:
                        await self.add_peer(peer)
            except Exception as e:
                self.logger.debug(f"Failed to exchange peers with {node}: {e}")

    async def tracker_announce_loop(self):
        while self._is_running:
            try:
                await self.announce_torrents_to_trackers()
            except Exception as e:
                self.logger.error(f"Tracker announce failed: {e}")
            await asyncio.sleep(self.tracker_announce_interval)

    async def announce_torrents_to_trackers(self):
        torrent_hashes = list(self.torrents.keys())
        if not torrent_hashes:
            return

        for tracker in self.get_known_trackers():
            try:
                async with aiohttp.ClientSession() as session:
                    params = {
                        'info_hash': ','.join(torrent_hashes),
                        'peer_id': self.server.node.id.hex(),
                        'port': self.port,
                        'uploaded': 0,
                        'downloaded': 0,
                        'left': 0,
                        'compact': 1
                    }
                    async with session.get(tracker, params=params) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            peers = self._decode_tracker_response(data)
                            for peer in peers:
                                await self.add_peer(peer)
            except Exception as e:
                self.logger.warning(f"Tracker announce to {tracker} failed: {e}")

    def _decode_tracker_response(self, data: bytes) -> List[str]:
        try:
            response = json.loads(data.decode())
            if 'peers' in response:
                return response['peers']
        except json.JSONDecodeError:
            pass

        try:
            if len(data) % 6 == 0:
                peers = []
                for i in range(0, len(data), 6):
                    ip = socket.inet_ntoa(data[i:i + 4])
                    port = struct.unpack('!H', data[i + 4:i + 6])[0]
                    peers.append(f"{ip}:{port}")
                return peers
        except Exception:
            pass

        return []

    def get_known_trackers(self) -> List[str]:
        return [
            'http://tracker.opentrackr.org:1337/announce',
            'udp://tracker.openbittorrent.com:80',
            'udp://tracker.opentrackr.org:1337/announce'
        ]

    async def create_torrent(self, file_path: Path) -> Dict[str, Any]:
        file_hash = self._calculate_file_hash(file_path)
        piece_size = 256 * 1024  # 256KB pieces
        pieces = await self._calculate_pieces(file_path, piece_size)

        torrent = {
            'info': {
                'name': file_path.name,
                'length': file_path.stat().st_size,
                'piece length': piece_size,
                'pieces': pieces,
                'private': 0
            },
            'announce': self.get_known_trackers(),
            'creation date': int(time.time()),
            'encoding': 'UTF-8'
        }

        self.torrents[file_hash] = torrent
        return torrent

    async def _calculate_pieces(self, file_path: Path, piece_size: int) -> str:
        sha1 = hashlib.sha1()
        pieces = []

        with open(file_path, 'rb') as f:
            while True:
                chunk = f.read(piece_size)
                if not chunk:
                    break
                sha1.update(chunk)
                pieces.append(sha1.digest())
                sha1 = hashlib.sha1()

        return b''.join(pieces).hex()

    async def download_torrent(self, torrent_info: Dict[str, Any]) -> bool:
        file_hash = self._calculate_torrent_hash(torrent_info)
        if file_hash in self.torrents:
            return True

        peers = await self._find_peers_for_torrent(torrent_info)
        if not peers:
            return False

        self.torrents[file_hash] = torrent_info
        asyncio.create_task(self._download_from_peers(torrent_info, peers))
        return True

    def _calculate_torrent_hash(self, torrent_info: Dict[str, Any]) -> str:
        info_dict = json.dumps(torrent_info['info'], sort_keys=True).encode()
        return hashlib.sha1(info_dict).hexdigest()

    async def _find_peers_for_torrent(self, torrent_info: Dict[str, Any]) -> List[str]:
        file_hash = self._calculate_torrent_hash(torrent_info)
        metadata = await self.get_value(file_hash)
        if metadata and 'nodes' in metadata:
            return metadata['nodes']
        return []

    async def _download_from_peers(self, torrent_info: Dict[str, Any], peers: List[str]):
        file_hash = self._calculate_torrent_hash(torrent_info)
        file_name = torrent_info['info']['name']
        file_path = self.files_dir / file_name

        for peer in peers:
            try:
                ip, port = peer.split(':')
                reader, writer = await asyncio.open_connection(ip, int(port))

                handshake = struct.pack('!B19s8s20s20s',
                                        19, b'BitTorrent protocol',
                                        b'\x00' * 8,
                                        bytes.fromhex(file_hash),
                                        bytes.fromhex(self.server.node.id.hex()))

                writer.write(handshake)
                await writer.drain()

                response = await reader.read(68)
                if len(response) != 68:
                    continue

                await self._download_pieces(reader, writer, torrent_info, file_path)
                writer.close()
                await writer.wait_closed()
                break
            except Exception as e:
                self.logger.debug(f"Failed to download from {peer}: {e}")

    async def _download_pieces(self, reader, writer, torrent_info, file_path):
        piece_length = torrent_info['info']['piece length']
        total_length = torrent_info['info']['length']
        pieces = torrent_info['info']['pieces']

        with open(file_path, 'wb') as f:
            for piece_index in range(0, len(pieces), 20):
                piece_hash = pieces[piece_index:piece_index + 20]
                offset = piece_index // 20 * piece_length
                length = min(piece_length, total_length - offset)

                request = struct.pack('!IbIII', 13, 6, piece_index, offset, length)
                writer.write(request)
                await writer.drain()

                response = await reader.read(4)
                if len(response) != 4:
                    break

                length = struct.unpack('!I', response)[0]
                data = await reader.read(length)
                if hashlib.sha1(data).digest() != piece_hash:
                    continue

                f.write(data)
                f.flush()

    async def add_peer(self, peer_address: str) -> bool:
        if peer_address in self.peers:
            return False

        self.peers.add(peer_address)
        self.logger.info(f"Added new peer: {peer_address}")

        for file_hash in self.torrents.keys():
            await self.update_peers_in_metadata(file_hash)

        return True

    async def update_peers_in_metadata(self, file_hash: str) -> bool:
        metadata = await self.get_value(file_hash)
        if not metadata:
            return False

        metadata["nodes"] = list(set(metadata.get("nodes", []) + list(self.peers)))
        return await self.set_value(file_hash, metadata)

    async def set_value(self, key: str, value: Dict[str, Any]) -> bool:
        try:
            serialized_value = json.dumps(value)
            await self.server.set(key, serialized_value)
            return True
        except Exception as e:
            self.logger.error(f"Failed to set value: {e}")
            return False

    async def get_value(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            serialized_value = await self.server.get(key)
            if serialized_value:
                return json.loads(serialized_value)
            return None
        except Exception as e:
            self.logger.error(f"Failed to get value: {e}")
            return None

    async def bootstrap(self, bootstrap_nodes: List[Tuple[str, int]]) -> bool:
        try:
            await self.server.bootstrap(bootstrap_nodes)
            return True
        except Exception as e:
            self.logger.error(f"Bootstrap failed: {e}")
            return False

    def _calculate_file_hash(self, file_path: Path) -> str:
        stat = file_path.stat()
        return digest(f"{file_path.name}{stat.st_size}{stat.st_mtime}").hex()