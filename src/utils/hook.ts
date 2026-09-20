import { Address, ethereum } from '@graphprotocol/graph-ts'

import { Hook } from '../types/schema'
import { ONE_BI, ZERO_BD, ZERO_BI } from './constants'

/**
 * Uniswap v4 hook permission flags.
 *
 * v4 packs these into the LOW 14 BITS of the hook contract's own address: a deployer mines a
 * CREATE2 salt until the resulting address carries exactly the bits for the callbacks it
 * implements (v4-core `src/libraries/Hooks.sol`). The consequence worth exploiting is that a
 * hook's entire capability set is derivable from its address -- no ABI, no eth_call, no
 * per-vendor data source, and it works on every network without a networks.json entry.
 *
 * Bit positions are copied verbatim from Hooks.sol; do not renumber them.
 */
const ALL_HOOK_MASK: i32 = (1 << 14) - 1

const BEFORE_INITIALIZE_FLAG: i32 = 1 << 13
const AFTER_INITIALIZE_FLAG: i32 = 1 << 12
const BEFORE_ADD_LIQUIDITY_FLAG: i32 = 1 << 11
const AFTER_ADD_LIQUIDITY_FLAG: i32 = 1 << 10
const BEFORE_REMOVE_LIQUIDITY_FLAG: i32 = 1 << 9
const AFTER_REMOVE_LIQUIDITY_FLAG: i32 = 1 << 8
const BEFORE_SWAP_FLAG: i32 = 1 << 7
const AFTER_SWAP_FLAG: i32 = 1 << 6
const BEFORE_DONATE_FLAG: i32 = 1 << 5
const AFTER_DONATE_FLAG: i32 = 1 << 4
const BEFORE_SWAP_RETURNS_DELTA_FLAG: i32 = 1 << 3
const AFTER_SWAP_RETURNS_DELTA_FLAG: i32 = 1 << 2
const AFTER_ADD_LIQUIDITY_RETURNS_DELTA_FLAG: i32 = 1 << 1
const AFTER_REMOVE_LIQUIDITY_RETURNS_DELTA_FLAG: i32 = 1 << 0

// Any of these means the hook can alter the amounts the PoolManager actually settles.
const RETURNS_DELTA_MASK: i32 =
  BEFORE_SWAP_RETURNS_DELTA_FLAG |
  AFTER_SWAP_RETURNS_DELTA_FLAG |
  AFTER_ADD_LIQUIDITY_RETURNS_DELTA_FLAG |
  AFTER_REMOVE_LIQUIDITY_RETURNS_DELTA_FLAG

/**
 * Read the 14 permission bits out of a hook address.
 *
 * Deliberately indexes the raw bytes rather than converting. `ByteArray.toU32()`/`toI32()`
 * would be the obvious call and both are wrong here: they `assert(false)` on any nonzero byte
 * past index 3 -- which a 20-byte address essentially always has, so the handler would abort --
 * and they read LITTLE-endian, so even on a truncated slice the byte order is reversed.
 * `BigInt.fromUnsignedBytes` carries the same endianness trap. Indexing sidesteps both, and
 * costs no allocation on a path that runs for every pool.
 */
export function hookPermissions(hookAddress: Address): i32 {
  const high: i32 = hookAddress[18]
  const low: i32 = hookAddress[19]
  return ((high << 8) | low) & ALL_HOOK_MASK
}

/** True when the hook holds any return-delta permission. See Hook.hasCustomAccounting. */
export function hookHasCustomAccounting(permissions: i32): boolean {
  return (permissions & RETURNS_DELTA_MASK) != 0
}

/**
 * Load the Hook for this address, creating and decoding it on first sight.
 *
 * Hookless pools (address zero) get a row too, so `Pool.hook` is uniformly populated and a
 * consumer can group every pool by hook without special-casing null. Their flag word is 0,
 * which reads correctly as "no permissions".
 */
export function loadOrCreateHook(hookAddress: Address, event: ethereum.Event): Hook {
  const id = hookAddress.toHexString()
  let hook = Hook.load(id)
  if (hook !== null) {
    return hook
  }

  hook = new Hook(id)
  const permissions = hookPermissions(hookAddress)
  hook.permissions = permissions

  hook.beforeInitialize = (permissions & BEFORE_INITIALIZE_FLAG) != 0
  hook.afterInitialize = (permissions & AFTER_INITIALIZE_FLAG) != 0
  hook.beforeAddLiquidity = (permissions & BEFORE_ADD_LIQUIDITY_FLAG) != 0
  hook.afterAddLiquidity = (permissions & AFTER_ADD_LIQUIDITY_FLAG) != 0
  hook.beforeRemoveLiquidity = (permissions & BEFORE_REMOVE_LIQUIDITY_FLAG) != 0
  hook.afterRemoveLiquidity = (permissions & AFTER_REMOVE_LIQUIDITY_FLAG) != 0
  hook.beforeSwap = (permissions & BEFORE_SWAP_FLAG) != 0
  hook.afterSwap = (permissions & AFTER_SWAP_FLAG) != 0
  hook.beforeDonate = (permissions & BEFORE_DONATE_FLAG) != 0
  hook.afterDonate = (permissions & AFTER_DONATE_FLAG) != 0
  hook.beforeSwapReturnsDelta = (permissions & BEFORE_SWAP_RETURNS_DELTA_FLAG) != 0
  hook.afterSwapReturnsDelta = (permissions & AFTER_SWAP_RETURNS_DELTA_FLAG) != 0
  hook.afterAddLiquidityReturnsDelta = (permissions & AFTER_ADD_LIQUIDITY_RETURNS_DELTA_FLAG) != 0
  hook.afterRemoveLiquidityReturnsDelta =
    (permissions & AFTER_REMOVE_LIQUIDITY_RETURNS_DELTA_FLAG) != 0
  hook.hasCustomAccounting = hookHasCustomAccounting(permissions)

  hook.poolCount = ZERO_BI
  hook.txCount = ZERO_BI
  hook.volumeUSD = ZERO_BD
  hook.untrackedVolumeUSD = ZERO_BD
  hook.feesUSD = ZERO_BD
  hook.totalValueLockedUSD = ZERO_BD
  hook.createdAtTimestamp = event.block.timestamp
  hook.createdAtBlockNumber = event.block.number

  return hook
}

/** Bump a hook's pool count. Call only once the pool is known to be persisted. */
export function incrementHookPoolCount(hook: Hook): void {
  hook.poolCount = hook.poolCount.plus(ONE_BI)
}
