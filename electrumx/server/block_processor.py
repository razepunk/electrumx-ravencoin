# Copyright (c) 2016-2017, Neil Booth
# Copyright (c) 2017, the ElectrumX authors
#
# All rights reserved.
#
# See the file "LICENCE" for information about the copyright
# and warranty status of this software.

'''Block prefetcher and chain processor.'''

import logging

import asyncio
import time
from asyncio import sleep

from aiorpcx import TaskGroup, CancelledError

import electrumx
from electrumx.server.daemon import DaemonError
from electrumx.lib.hash import hash_to_hex_str, HASHX_LEN
from electrumx.lib.script import is_unspendable_legacy, is_unspendable_genesis
from electrumx.lib.util import (
    class_logger, pack_le_uint32, pack_le_uint64, unpack_le_uint64
)
from electrumx.server.db import FlushData
from electrumx.lib.assets import is_asset_script, TX_TRANSFER_ASSET, TX_NEW_ASSET, TX_REISSUE_ASSET
from electrumx.lib.addresses import public_key_to_address

# We can safely assume that TX's to these addresses will never come out
# Therefore we don't need to store them in the database
BURN_ADDRESSES = [
    'RXissueAssetXXXXXXXXXXXXXXXXXhhZGt'
    'RXReissueAssetXXXXXXXXXXXXXXVEFAWu',
    'RXissueSubAssetXXXXXXXXXXXXXWcwhwL',
    'RXissueUniqueAssetXXXXXXXXXXWEAe58',
    'RXBurnXXXXXXXXXXXXXXXXXXXXXXWUo9FV',
]

class Prefetcher:
    '''Prefetches blocks (in the forward direction only).'''

    def __init__(self, daemon, coin, blocks_event):
        self.logger = class_logger(__name__, self.__class__.__name__)
        self.daemon = daemon
        self.coin = coin
        self.blocks_event = blocks_event
        self.blocks = []
        self.caught_up = False
        # Access to fetched_height should be protected by the semaphore
        self.fetched_height = None
        self.semaphore = asyncio.Semaphore()
        self.refill_event = asyncio.Event()
        # The prefetched block cache size.  The min cache size has
        # little effect on sync time.
        self.cache_size = 0
        self.min_cache_size = 10 * 1024 * 1024
        # This makes the first fetch be 10 blocks
        self.ave_size = self.min_cache_size // 10
        self.polling_delay = 5

    async def main_loop(self, bp_height):
        '''Loop forever polling for more blocks.'''
        await self.reset_height(bp_height)
        while True:
            try:
                # Sleep a while if there is nothing to prefetch
                await self.refill_event.wait()
                if not await self._prefetch_blocks():
                    await sleep(self.polling_delay)
            except DaemonError as e:
                self.logger.info(f'ignoring daemon error: {e}')
            except CancelledError as e:
                self.logger.info(f'cancelled; prefetcher stopping {e}')
                raise
            except Exception:   # pylint:disable=W0703
                self.logger.exception('ignoring unexpected exception')

    def get_prefetched_blocks(self):
        '''Called by block processor when it is processing queued blocks.'''
        blocks = self.blocks
        self.blocks = []
        self.cache_size = 0
        self.refill_event.set()
        return blocks

    async def reset_height(self, height):
        '''Reset to prefetch blocks from the block processor's height.

        Used in blockchain reorganisations.  This coroutine can be
        called asynchronously to the _prefetch_blocks coroutine so we
        must synchronize with a semaphore.
        '''
        async with self.semaphore:
            self.blocks.clear()
            self.cache_size = 0
            self.fetched_height = height
            self.refill_event.set()

        daemon_height = await self.daemon.height()
        behind = daemon_height - height
        if behind > 0:
            self.logger.info('catching up to daemon height {:,d} '
                             '({:,d} blocks behind)'
                             .format(daemon_height, behind))
        else:
            self.logger.info('caught up to daemon height {:,d}'
                             .format(daemon_height))

    async def _prefetch_blocks(self):
        '''Prefetch some blocks and put them on the queue.

        Repeats until the queue is full or caught up.
        '''
        daemon = self.daemon
        daemon_height = await daemon.height()
        async with self.semaphore:
            while self.cache_size < self.min_cache_size:
                first = self.fetched_height + 1
                # Try and catch up all blocks but limit to room in cache.
                cache_room = max(self.min_cache_size // self.ave_size, 1)
                count = min(daemon_height - self.fetched_height, cache_room)
                # Don't make too large a request
                count = min(self.coin.max_fetch_blocks(first), max(count, 0))
                if not count:
                    self.caught_up = True
                    return False

                hex_hashes = await daemon.block_hex_hashes(first, count)
                if self.caught_up:
                    self.logger.info('new block height {:,d} hash {}'
                                     .format(first + count-1, hex_hashes[-1]))
                blocks = await daemon.raw_blocks(hex_hashes)

                assert count == len(blocks)

                # Special handling for genesis block
                if first == 0:
                    blocks[0] = self.coin.genesis_block(blocks[0])
                    self.logger.info('verified genesis block with hash {}'
                                     .format(hex_hashes[0]))

                # Update our recent average block size estimate
                size = sum(len(block) for block in blocks)
                if count >= 10:
                    self.ave_size = size // count
                else:
                    self.ave_size = (size + (10 - count) * self.ave_size) // 10

                self.blocks.extend(blocks)
                self.cache_size += size
                self.fetched_height += count
                self.blocks_event.set()

        self.refill_event.clear()
        return True


class ChainError(Exception):
    '''Raised on error processing blocks.'''


class BlockProcessor:
    '''Process blocks and update the DB state to match.

    Employ a prefetcher to prefetch blocks in batches for processing.
    Coordinate backing up in case of chain reorganisations.
    '''

    def __init__(self, env, db, daemon, notifications):
        self.env = env
        self.db = db
        self.daemon = daemon
        self.notifications = notifications

        # Set when there is block processing to do, e.g. when new blocks come in, or a
        # reorg is needed.
        self.blocks_event = asyncio.Event()

        # If the lock is successfully acquired, in-memory chain state
        # is consistent with self.height
        self.state_lock = asyncio.Lock()

        # Signalled after backing up during a reorg
        self.backed_up_event = asyncio.Event()

        self.coin = env.coin
        self.prefetcher = Prefetcher(daemon, env.coin, self.blocks_event)
        self.logger = class_logger(__name__, self.__class__.__name__)

        # Meta
        self.next_cache_check = 0
        self.touched = set()
        self.reorg_count = None
        self.height = -1
        self.tip = None
        self.tx_count = 0
        self.asset_count = 0
        self._caught_up_event = None

        # Caches of unflushed items.
        self.headers = []
        self.tx_hashes = []
        self.undo_infos = []

        # UTXO cache
        self.utxo_cache = {}
        self.db_deletes = []

        # Asset cache

        # Same as utxo cache but for assets.
        # All keys in this dict will also be in the
        # utxo_cache because assets are normal tx's with no RVN value
        self.asset_cache = {}

        # Same as above.
        self.asset_deletes = []

        self.asset_undo_infos = []

        # A dict of the asset name -> asset data
        self.asset_data_new = {}
        self.asset_data_reissued = {}

        self.asset_data_undo_infos = []
        self.asset_data_deletes = []

        # To notify clients about reissuances
        self.asset_touched = set()

    async def run_with_lock(self, coro):
        # Shielded so that cancellations from shutdown don't lose work.  Cancellation will
        # cause fetch_and_process_blocks to block on the lock in flush(), the task completes,
        # and then the data is flushed.  We also don't want user-signalled reorgs to happen
        # in the middle of processing blocks; they need to wait.
        async def run_locked():
            async with self.state_lock:
                return await coro
        return await asyncio.shield(run_locked())

    def schedule_reorg(self, count):
        '''A count >= 0 is a user-forced reorg; < 0 is a natural reorg.'''
        self.reorg_count = count
        self.blocks_event.set()

    async def _reorg_chain(self, count):
        '''Handle a chain reorganisation.

        Count is the number of blocks to simulate a reorg, or None for
        a real reorg.'''
        if count < 0:
            self.logger.info('chain reorg detected')
        else:
            self.logger.info(f'faking a reorg of {count:,d} blocks')
        await self.flush(True)

        async def get_raw_block(hex_hash, height):
            try:
                block = self.db.read_raw_block(height)
                self.logger.info(f'read block {hex_hash} at height {height:,d} from disk')
            except FileNotFoundError:
                block = await self.daemon.raw_blocks([hex_hash])[0]
                self.logger.info(f'obtained block {hex_hash} at height {height:,d} from daemon')
            return block

        _start, height, hashes = await self._reorg_hashes(count)
        hex_hashes = [hash_to_hex_str(block_hash) for block_hash in hashes]
        for hex_hash in reversed(hex_hashes):
            raw_block = await get_raw_block(hex_hash, height)
            await self._backup_block(raw_block)
            # self.touched can include other addresses which is harmless, but remove None.
            self.touched.discard(None)
            self.db.flush_backup(self.flush_data(), self.touched)
            height -= 1

        self.logger.info('backed up to height {:,d}'.format(self.height))

        await self.prefetcher.reset_height(self.height)
        self.backed_up_event.set()
        self.backed_up_event.clear()

    async def _reorg_hashes(self, count):
        '''Return a pair (start, last, hashes) of blocks to back up during a
        reorg.

        The hashes are returned in order of increasing height.  Start
        is the height of the first hash, last of the last.
        '''
        start, count = await self._calc_reorg_range(count)
        last = start + count - 1
        s = '' if count == 1 else 's'
        self.logger.info(f'chain was reorganised replacing {count:,d} '
                         f'block{s} at heights {start:,d}-{last:,d}')

        return start, last, await self.db.fs_block_hashes(start, count)

    async def _calc_reorg_range(self, count):
        '''Calculate the reorg range'''

        def diff_pos(hashes1, hashes2):
            '''Returns the index of the first difference in the hash lists.
            If both lists match returns their length.'''
            for n, (hash1, hash2) in enumerate(zip(hashes1, hashes2)):
                if hash1 != hash2:
                    return n
            return len(hashes)

        if count < 0:
            # A real reorg
            start = self.height - 1
            count = 1
            while start > 0:
                hashes = await self.db.fs_block_hashes(start, count)
                hex_hashes = [hash_to_hex_str(hash) for hash in hashes]
                d_hex_hashes = await self.daemon.block_hex_hashes(start, count)
                n = diff_pos(hex_hashes, d_hex_hashes)
                if n > 0:
                    start += n
                    break
                count = min(count * 2, start)
                start -= count

            count = (self.height - start) + 1
        else:
            start = (self.height - count) + 1

        return start, count

    def estimate_txs_remaining(self):
        # Try to estimate how many txs there are to go
        daemon_height = self.daemon.cached_height()
        coin = self.coin
        tail_count = daemon_height - max(self.height, coin.TX_COUNT_HEIGHT)
        # Damp the initial enthusiasm
        realism = max(2.0 - 0.9 * self.height / coin.TX_COUNT_HEIGHT, 1.0)
        return (tail_count * coin.TX_PER_BLOCK +
                max(coin.TX_COUNT - self.tx_count, 0)) * realism

    # - Flushing
    def flush_data(self):
        '''The data for a flush.  The lock must be taken.'''
        assert self.state_lock.locked()
        return FlushData(self.height, self.tx_count, self.headers,
                         self.tx_hashes, self.undo_infos, self.utxo_cache,
                         self.db_deletes, self.tip,
                         self.asset_cache, self.asset_deletes,
                         self.asset_data_new, self.asset_data_reissued,
                         self.asset_undo_infos, self.asset_data_undo_infos,
                         self.asset_data_deletes, self.asset_count)

    async def flush(self, flush_utxos):
        self.db.flush_dbs(self.flush_data(), flush_utxos, self.estimate_txs_remaining)
        self.next_cache_check = time.monotonic() + 30

    def check_cache_size(self):
        '''Flush a cache if it gets too big.'''
        # Good average estimates based on traversal of subobjects and
        # requesting size from Python (see deep_getsizeof).

        # TODO: Add undo info checks

        one_MB = 1000*1000
        utxo_cache_size = len(self.utxo_cache) * 205
        db_deletes_size = len(self.db_deletes) * 57
        hist_cache_size = self.db.history.unflushed_memsize()
        # Roughly ntxs * 32 + nblocks * 42
        tx_hash_size = ((self.tx_count - self.db.fs_tx_count) * 32
                        + (self.height - self.db.fs_height) * 42)

        #TODO Fix these approximations
        asset_cache_size = len(self.asset_cache) * 235 #Added 30 bytes for the max name length
        asset_deletes_size = len(self.asset_deletes) * 57
        asset_data_new_size = len(self.asset_data_new) * 232
        asset_data_reissue_size = len(self.asset_data_reissued) * 232

        utxo_MB = (db_deletes_size + utxo_cache_size) // one_MB
        hist_MB = (hist_cache_size + tx_hash_size) // one_MB
        asset_MB = (asset_data_new_size + asset_data_reissue_size +
                    asset_deletes_size + asset_cache_size) // one_MB

        self.logger.info('our height: {:,d} daemon: {:,d} '
                         'UTXOs {:,d}MB hist {:,d}MB assets {:,d}MB'
                         .format(self.height, self.daemon.cached_height(),
                                 utxo_MB, hist_MB, asset_MB))

        # Flush history if it takes up over 20% of cache memory.
        # Flush UTXOs once they take up 80% of cache memory.
        cache_MB = self.env.cache_MB
        if asset_MB + utxo_MB + hist_MB >= cache_MB or hist_MB >= cache_MB // 5:
            return (utxo_MB + asset_MB) >= cache_MB * 4 // 5
        return None

    async def _advance_blocks(self, raw_blocks):
        '''Process the list of raw blocks passed.  Detects and handles reorgs.'''
        start = time.monotonic()
        first = self.height + 1
        for n, raw_block in enumerate(raw_blocks):
            block = self.coin.block(raw_block, first + n)
            if self.coin.header_prevhash(block.header) != self.tip:
                self.schedule_reorg(-1)
                return
            await self._advance_block(block)
        end = time.monotonic()

        if not self.db.first_sync:
            s = '' if len(raw_blocks) == 1 else 's'
            blocks_size = sum(len(block) for block in raw_blocks) / 1_000_000
            self.logger.info(f'processed {len(raw_blocks):,d} block{s} size {blocks_size:.2f} MB '
                             f'in {end - start:.1f}s')

        # If caught up, flush everything as client queries are performed on the DB,
        # otherwise check at regular intervals.
        if self.height == self.daemon.cached_height():
            await self.flush(True)
            await self._on_caught_up()
        elif end > self.next_cache_check:
            flush_arg = self.check_cache_size()
            if flush_arg is not None:
                await self.flush(flush_arg)

        if self._caught_up_event.is_set():
            await self.notifications.on_block(self.touched, self.height, self.asset_touched)

        self.touched = set()
        self.asset_touched = set()

    async def _advance_block(self, block):
        '''Advance once block.  It is already verified they correctly connect onto our tip.'''
        min_height = self.db.min_undo_height(self.daemon.cached_height())
        height = self.height + 1

        is_unspendable = (is_unspendable_genesis if height >= self.coin.GENESIS_ACTIVATION
                          else is_unspendable_legacy)

        (undo_info, asset_undo_info, asset_meta_undo_info) = self.advance_txs(block.transactions, is_unspendable)

        if height >= min_height:
            self.undo_infos.append((undo_info, height))
            self.asset_undo_infos.append((asset_undo_info, height))
            self.asset_data_undo_infos.append((asset_meta_undo_info, height))
            self.db.write_raw_block(block.raw, height)

        self.height = height
        self.headers.append(block.header)
        self.tip = self.coin.header_hash(block.header)

        await sleep(0)

    def advance_txs(self, txs, is_unspendable):
        self.tx_hashes.append(b''.join(tx_hash for tx, tx_hash in txs))

        # Use local vars for speed in the loops
        undo_info = []
        asset_undo_info = []
        asset_meta_undo_info = []
        tx_num = self.tx_count
        asset_num = self.asset_count
        script_hashX = self.coin.hashX_from_script
        put_utxo = self.utxo_cache.__setitem__
        put_asset = self.asset_cache.__setitem__
        put_asset_data_new = self.asset_data_new.__setitem__
        put_asset_data_reissued = self.asset_data_reissued.__setitem__
        spend_utxo = self.spend_utxo
        spend_asset = self.spend_asset
        undo_info_append = undo_info.append
        asset_undo_info_append = asset_undo_info.append
        asset_meta_undo_info_append = asset_meta_undo_info.append
        update_touched = self.touched.update
        hashXs_by_tx = []
        append_hashXs = hashXs_by_tx.append
        to_le_uint32 = pack_le_uint32
        to_le_uint64 = pack_le_uint64

        for tx, tx_hash in txs:
            hashXs = []
            append_hashX = hashXs.append
            tx_numb = to_le_uint64(tx_num)[:5]

            # Spend the inputs
            for txin in tx.inputs:
                if txin.is_generation(): # Don't spend block rewards
                    continue
                cache_value = spend_utxo(txin.prev_hash, txin.prev_idx)
                asset_cache_value = spend_asset(txin.prev_hash, txin.prev_idx)
                undo_info_append(cache_value)
                asset_undo_info_append(asset_cache_value)
                append_hashX(cache_value[:-13])

            # Add the new UTXOs
            for idx, txout in enumerate(tx.outputs):
                # Ignore unspendable outputs
                if is_unspendable(txout.pk_script):
                    continue

                # ASSETS CHANGE THE SCRIPT!!!!!!!
                # THIS TOOK ME SO FUCKING LONG TO FIND!!!!
                # GOD DAMN HASHES
                # First find the type!
                end_point = 23 \
                    if len(txout.pk_script) > 22 and txout.pk_script[0] == 0xa9 and \
                        txout.pk_script[1] == 0x14 and txout.pk_script[22] == 0x87 \
                    else 25

                # Get the hashX
                hashX = script_hashX(txout.pk_script[:end_point]) # Only hash the "normal" part

                # Convert depricated script pubkeys into script pubkeyhashes for db purposes
                pubkey_len = txout.pk_script[0]
                if len(txout.pk_script) > pubkey_len + 1 and txout.pk_script[pubkey_len + 1] == 0xac:
                    pubkey = txout.pk_script[1:pubkey_len + 1]
                    try:
                        addr = public_key_to_address(pubkey, self.coin.P2PKH_VERBYTE)
                        hashX = self.coin.address_to_hashX(addr)
                    except:
                        pass

                append_hashX(hashX)
                put_utxo(tx_hash + to_le_uint32(idx),
                         hashX + tx_numb + to_le_uint64(txout.value))

                asset_info = is_asset_script(txout.pk_script)
                # For testing purposes. TODO: Make gooder
                if asset_info is not None:
                    asset_num += 1
                    start = asset_info[2]
                    asset_name_len = txout.pk_script[start]
                    asset_name = txout.pk_script[start + 1:(start + 1 + asset_name_len)]
                    if asset_info[1]:
                        # This is an ownership asset, just give it "1"
                        # Ownership assets do not have any metadata, just assign it a value of 1
                        put_asset(tx_hash + to_le_uint32(idx),
                                  hashX + tx_numb + to_le_uint64(100_000_000) +
                                  txout.pk_script[start:(start + 1 + asset_name_len)])
                    else:  # Not an owner asset
                        sat_amt = int.from_bytes(txout.pk_script[(start + 1 + asset_name_len):
                                                                 (start + 9 + asset_name_len)],
                                                 byteorder='little')
                        put_asset(tx_hash + to_le_uint32(idx),
                                  hashX + tx_numb + to_le_uint64(sat_amt) +
                                  txout.pk_script[start:(start + 1 + asset_name_len)])
                        if asset_info[0] != TX_TRANSFER_ASSET:
                            # div_amt = txout.pk_script[start + 9 + asset_name_len]
                            # reissue = False if txout.pk_script[start + 10 + asset_name_len] == 0 else True
                            if asset_info[0] != TX_REISSUE_ASSET:
                                # This only happens when creating new assets
                                has_ifps = False if txout.pk_script[start + 11 + asset_name_len] == 0 else True
                                ifps = txout.pk_script[
                                       start + 12 + asset_name_len:start + 46 + asset_name_len] if has_ifps else None
                                asset_data = txout.pk_script[start + 9 + asset_name_len:start + 12 + asset_name_len]
                                if has_ifps:
                                    asset_data += ifps
                                asset_meta_undo_info_append(
                                    asset_name_len.to_bytes(1, 'big') + asset_name + b'\0')
                                put_asset_data_new(asset_name, asset_data)
                            else:
                                # When reissuing
                                div_amt = txout.pk_script[start + 9 + asset_name_len]
                                reissuable = txout.pk_script[start + 10 + asset_name_len]
                                asset_data = b''

                                # Quicker check, but it's far more likely to be in the db
                                old_data = self.asset_data_new.pop(asset_name, None)
                                if old_data is None:
                                    old_data = self.asset_data_reissued.pop(asset_name, None)
                                if old_data is None:
                                    old_data = self.db.asset_info_db.get(asset_name)
                                assert old_data is not None  # If reissuing, we should have it

                                asset_meta_undo_info_append(
                                    asset_name_len.to_bytes(1, 'big') + asset_name +
                                    len(old_data).to_bytes(1, 'big') + old_data)

                                if div_amt == 0xff:  # Unchanged division amount
                                    asset_data += old_data[0].to_bytes(1, 'big')
                                else:
                                    asset_data += div_amt.to_bytes(1, 'big')
                                asset_data += reissuable.to_bytes(1, 'big')

                                check_if_ipfs = txout.pk_script[start + 11 + asset_name_len]
                                if check_if_ipfs == 0x75:  # End of script
                                    asset_data += b'\0'
                                elif check_if_ipfs != 0:
                                    # I am assuming that if a 0 comes next,
                                    # it is the same as creating an asset
                                    asset_data += b'\x01'
                                    asset_data += txout.pk_script[
                                                  start + 11 + asset_name_len:start + 45 + asset_name_len]
                                else:
                                    asset_data += b'\0'
                                if asset_name[-1] != 33: # Not an ownership asset
                                    self.asset_touched.update(asset_name)
                                put_asset_data_reissued(asset_name, asset_data)

            append_hashXs(hashXs)
            update_touched(hashXs)
            tx_num += 1

        self.db.history.add_unflushed(hashXs_by_tx, self.tx_count)

        self.asset_count = asset_num
        self.tx_count = tx_num
        self.db.tx_counts.append(tx_num)

        # Assets aren't always in tx's... remove None types
        asset_undo_info = [i for i in asset_undo_info if i]

        return undo_info, asset_undo_info, asset_meta_undo_info

    async def _backup_block(self, raw_block):
        '''Backup the raw block and flush.

        The blocks should be in order of decreasing height, starting at.  self.height.  A
        flush is performed once the blocks are backed up.
        '''
        self.db.assert_flushed(self.flush_data())
        assert self.height > 0
        genesis_activation = self.coin.GENESIS_ACTIVATION

        coin = self.coin

        # Check and update self.tip
        block = coin.block(raw_block, self.height)
        header_hash = coin.header_hash(block.header)
        if header_hash != self.tip:
            raise ChainError('backup block {} not tip {} at height {:,d}'
                             .format(hash_to_hex_str(header_hash),
                                     hash_to_hex_str(self.tip),
                                     self.height))
        self.tip = coin.header_prevhash(block.header)
        is_unspendable = (is_unspendable_genesis if self.height >= genesis_activation
                          else is_unspendable_legacy)
        self._backup_txs(block.transactions, is_unspendable)
        self.height -= 1
        self.db.tx_counts.pop()

        await sleep(0)

    def _backup_txs(self, txs, is_unspendable):
        #TODO: Asset meta undo infos

        # Prevout values, in order down the block (coinbase first if present)
        # undo_info is in reverse block order
        undo_info = self.db.read_undo_info(self.height)
        asset_undo_info = self.db.read_asset_undo_info(self.height)
        if undo_info is None or asset_undo_info is None:
            raise ChainError('no undo information found for height {:,d}'
                             .format(self.height))

        asset_meta_undo_info = self.db.read_asset_meta_undo_info(self.height)
        while asset_meta_undo_info: # Stops when None or empty
            name_len = asset_meta_undo_info[0]
            name = asset_meta_undo_info[1:name_len+1]
            data_len = asset_meta_undo_info[name_len+1]
            if data_len == 0:
                self.asset_data_deletes.append(name)
            else:
                data = asset_meta_undo_info[name_len+1:name_len+1+data_len]
                self.asset_data_reissued[name] = data
            asset_meta_undo_info = asset_meta_undo_info[name_len+2+data_len:]

        n = len(undo_info)
        asset_n = len(asset_undo_info)

        # Use local vars for speed in the loops
        put_utxo = self.utxo_cache.__setitem__
        spend_utxo = self.spend_utxo
        touched = self.touched
        undo_entry_len = 13 + HASHX_LEN

        # n is our pointer.
        # Items in our list are ordered, but we want them backwards.

        # Value of the asset cache is:
        # tx_hash + u32 idx + HASHX + TX_NUMB + SATS IN U64 + 1 BYTE OF LEN + NAME
        # 32 + 4 + HASHX_LEN BYTES + 5 BYTES + 8 BYTES + 1 BYTE + VAR BYTES
        def find_asset_undo_len(max):
            assert max <= len(asset_undo_info)

            if max == 0:
                return 0
            else:
                def val_len(ptr):
                    name_len = asset_undo_info[ptr+HASHX_LEN+13+32+4]
                    return name_len + HASHX_LEN + 14 + 32 + 4

                last_val_ptr = 0
                while True:
                    next_data = last_val_ptr + val_len(last_val_ptr)
                    if next_data >= max:
                        break
                    last_val_ptr += val_len(last_val_ptr)
                assert next_data == max
                return max - last_val_ptr

        assets = 0
        put_asset = self.asset_cache.__setitem__
        spend_asset = self.spend_asset

        for tx, tx_hash in reversed(txs):
            for idx, txout in enumerate(tx.outputs):
                # Spend the TX outputs.  Be careful with unspendable
                # outputs - we didn't save those in the first place.
                if is_unspendable(txout.pk_script):
                    continue

                cache_value = spend_utxo(tx_hash, idx)

                # Since we add assets in the UTXO's normally, when backing up
                # Remove them in the spend.
                if spend_asset(tx_hash, idx):
                    assets += 1

                # All assets will be in the normal utxo cache
                touched.add(cache_value[:-13])

            # Restore the inputs
            for txin in reversed(tx.inputs):
                prev_idx_bytes = pack_le_uint32(txin.prev_idx)

                if txin.is_generation():
                    continue
                n -= undo_entry_len
                undo_item = undo_info[n:n + undo_entry_len]
                put_utxo(txin.prev_hash + pack_le_uint32(txin.prev_idx), undo_item)
                touched.add(undo_item[:-13])

                asset_undo_entry_len = find_asset_undo_len(asset_n)
                new_asset_n = asset_n - asset_undo_entry_len
                if new_asset_n >= 0 and asset_undo_entry_len > 0:
                    undo_item = asset_undo_info[new_asset_n:new_asset_n+asset_undo_entry_len]
                    if undo_item[:32] == txin.prev_hash and undo_item[32:36] == prev_idx_bytes:
                        put_asset(txin.prev_hash + prev_idx_bytes, undo_item[36:])
                        asset_n = new_asset_n

        assert n == 0
        assert asset_n == 0
        self.tx_count -= len(txs)
        self.asset_count -= assets

    '''An in-memory UTXO cache, representing all changes to UTXO state
    since the last DB flush.

    We want to store millions of these in memory for optimal
    performance during initial sync, because then it is possible to
    spend UTXOs without ever going to the database (other than as an
    entry in the address history, and there is only one such entry per
    TX not per UTXO).  So store them in a Python dictionary with
    binary keys and values.

      Key:    TX_HASH + TX_IDX           (32 + 4 = 36 bytes)
      Value:  HASHX + TX_NUM + VALUE     (11 + 5 + 8 = 24 bytes)

    That's 60 bytes of raw data in-memory.  Python dictionary overhead
    means each entry actually uses about 205 bytes of memory.  So
    almost 5 million UTXOs can fit in 1GB of RAM.  There are
    approximately 42 million UTXOs on bitcoin mainnet at height
    433,000.

    Semantics:

      add:   Add it to the cache dictionary.

      spend: Remove it if in the cache dictionary.  Otherwise it's
             been flushed to the DB.  Each UTXO is responsible for two
             entries in the DB.  Mark them for deletion in the next
             cache flush.

    The UTXO database format has to be able to do two things efficiently:

      1.  Given an address be able to list its UTXOs and their values
          so its balance can be efficiently computed.

      2.  When processing transactions, for each prevout spent - a (tx_hash,
          idx) pair - we have to be able to remove it from the DB.  To send
          notifications to clients we also need to know any address it paid
          to.

    To this end we maintain two "tables", one for each point above:

      1.  Key: b'u' + address_hashX + tx_idx + tx_num
          Value: the UTXO value as a 64-bit unsigned integer

      2.  Key: b'h' + compressed_tx_hash + tx_idx + tx_num
          Value: hashX

    The compressed tx hash is just the first few bytes of the hash of
    the tx in which the UTXO was created.  As this is not unique there
    will be potential collisions so tx_num is also in the key.  When
    looking up a UTXO the prefix space of the compressed hash needs to
    be searched and resolved if necessary with the tx_num.  The
    collision rate is low (<0.1%).
    '''

    def spend_utxo(self, tx_hash, tx_idx):
        '''Spend a UTXO and return the 33-byte value.

        If the UTXO is not in the cache it must be on disk.  We store
        all UTXOs so not finding one indicates a logic error or DB
        corruption.
        '''
        # Fast track is it being in the cache
        idx_packed = pack_le_uint32(tx_idx)
        cache_value = self.utxo_cache.pop(tx_hash + idx_packed, None)
        if cache_value:
            return cache_value

        # Spend it from the DB.

        # Key: b'h' + compressed_tx_hash + tx_idx + tx_num
        # Value: hashX
        prefix = b'h' + tx_hash[:4] + idx_packed
        candidates = {db_key: hashX for db_key, hashX
                      in self.db.utxo_db.iterator(prefix=prefix)}

        for hdb_key, hashX in candidates.items():
            tx_num_packed = hdb_key[-5:]

            if len(candidates) > 1:
                tx_num, = unpack_le_uint64(tx_num_packed + bytes(3))
                fs_hash, _height = self.db.fs_tx_hash(tx_num)
                if fs_hash != tx_hash:
                    assert fs_hash is not None  # Should always be found
                    continue

            # Key: b'u' + address_hashX + tx_idx + tx_num
            # Value: the UTXO value as a 64-bit unsigned integer
            udb_key = b'u' + hashX + hdb_key[-9:]
            utxo_value_packed = self.db.utxo_db.get(udb_key)
            if utxo_value_packed:
                # Remove both entries for this UTXO
                self.db_deletes.append(hdb_key)
                self.db_deletes.append(udb_key)
                return hashX + tx_num_packed + utxo_value_packed

        raise ChainError('UTXO {} / {:,d} not found in "h" table'
                         .format(hash_to_hex_str(tx_hash), tx_idx))

    def spend_asset(self, tx_hash, tx_idx):
        # TODO: Find a way to make this faster
        # Fast track is it being in the cache
        idx_packed = pack_le_uint32(tx_idx)
        cache_value = self.asset_cache.pop(tx_hash + idx_packed, None)
        if cache_value:
            return tx_hash+idx_packed+cache_value

        # Spend it from the DB.

        # Key: b'h' + compressed_tx_hash + tx_idx + tx_num
        # Value: hashX
        prefix = b'h' + tx_hash[:4] + idx_packed
        candidates = {db_key: hashX for db_key, hashX
                      in self.db.asset_db.iterator(prefix=prefix)}

        for hdb_key, hashX in candidates.items():
            tx_num_packed = hdb_key[-5:]

            if len(candidates) > 1:
                tx_num, = unpack_le_uint64(tx_num_packed + bytes(3))
                # Assets are on txs
                fs_hash, _height = self.db.fs_tx_hash(tx_num)
                if fs_hash != tx_hash:
                    assert fs_hash is not None  # Should always be found
                    continue

            # Key: b'u' + address_hashX + tx_idx + tx_num
            # Value: the asset amt and name
            udb_key = b'u' + hashX + hdb_key[-9:]
            value = self.db.asset_db.get(udb_key)
            if value:
                # Remove both entries for this Asset
                self.asset_deletes.append(hdb_key)
                self.asset_deletes.append(udb_key)
                return tx_hash + idx_packed + hashX + tx_num_packed + value

        # Asset doesn't need to be found
        # raise ChainError('UTXO {} / {:,d} not found in "h" table'
                        # .format(hash_to_hex_str(tx_hash), tx_idx))

    async def _process_blocks(self):
        '''Loop forever processing blocks as they arrive.'''
        async def process_event():
            '''Perform a pending reorg or process prefetched blocks.'''
            if self.reorg_count is not None:
                await self._reorg_chain(self.reorg_count)
                self.reorg_count = None
                # Prefetcher block cache cleared so nothing to process
            else:
                blocks = self.prefetcher.get_prefetched_blocks()
                await self._advance_blocks(blocks)

        # This must be done to set state before the main loop
        if self.height == self.daemon.cached_height():
            await self._on_caught_up()

        while True:
            await self.blocks_event.wait()
            self.blocks_event.clear()
            await self.run_with_lock(process_event())

    async def _on_caught_up(self):
        if not self._caught_up_event.is_set():
            self._caught_up_event.set()
            self.logger.info(f'caught up to height {self.height}')
            # Flush everything but with first_sync->False state.
            first_sync = self.db.first_sync
            self.db.first_sync = False
            if first_sync:
                self.logger.info(f'{electrumx.version} synced to height {self.height:,d}')
            # Reopen for serving
            await self.db.open_for_serving()

    async def _first_open_dbs(self):
        await self.db.open_for_sync()
        self.height = self.db.db_height
        self.tip = self.db.db_tip
        self.tx_count = self.db.db_tx_count
        self.asset_count = self.db.db_asset_count

    # --- External API

    async def fetch_and_process_blocks(self, caught_up_event):
        '''Fetch, process and index blocks from the daemon.

        Sets caught_up_event when first caught up.  Flushes to disk
        and shuts down cleanly if cancelled.

        This is mainly because if, during initial sync ElectrumX is
        asked to shut down when a large number of blocks have been
        processed but not written to disk, it should write those to
        disk before exiting, as otherwise a significant amount of work
        could be lost.
        '''
        self._caught_up_event = caught_up_event
        await self._first_open_dbs()
        try:
            async with TaskGroup() as group:
                await group.spawn(self.prefetcher.main_loop(self.height))
                await group.spawn(self._process_blocks())

            group.result
        # Don't flush for arbitrary exceptions as they might be a cause or consequence of
        # corrupted data
        except CancelledError:
            self.logger.info('flushing to DB for a clean shutdown...')
            await self.run_with_lock(self.flush(True))
            self.logger.info('flushed cleanly')
        except Exception:
            logging.exception('Critical Block Processor Error:')
            raise

    def force_chain_reorg(self, count):
        '''Force a reorg of the given number of blocks.

        Returns True if a reorg is queued, false if not caught up.
        '''
        if self._caught_up_event.is_set():
            self.schedule_reorg(count)
            return True
        return False
