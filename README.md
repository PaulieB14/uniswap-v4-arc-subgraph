# Uniswap V4 on Arc — subgraph

A fork of [Uniswap/v4-subgraph](https://github.com/Uniswap/v4-subgraph) (GPL-3.0) for **Arc
Mainnet** (`eip155:5042`), Circle's USDC-native L1.

It is not just a config entry. Arc breaks assumptions that hold on every other chain, and two of
the failures are silent — a subgraph that deploys, syncs, advances its block number and reports
`hasIndexingErrors: false` while producing wrong or empty data. Both are documented below, along
with a `Hook` entity that upstream does not have.

| | |
|---|---|
| Studio | `https://api.studio.thegraph.com/query/111767/uniswap-v4---arc/<version>` |
| Network | [`7xLobfNG9xx8yM5hkxagRuPmdjCJP4Kj9vtomrn9LrRM`](https://thegraph.com/explorer/subgraphs/7xLobfNG9xx8yM5hkxagRuPmdjCJP4Kj9vtomrn9LrRM) |
| PoolManager | `0x8366a39cc670b4001a1121b8f6a443a643e40951` |
| PositionManager | `0x6049c9a0e26405c0985f9e3685c87d0ae917f82b` |
| Start block | 1948011 |

---

## Contents

- [The `Hook` entity](#the-hook-entity) — permissions decoded from the address
  - [Permission flags](#permission-flags) · [Arc's hook landscape](#what-arcs-hook-landscape-actually-looks-like) · [Hook fields](#hook-fields)
- [Indexing on Arc](#indexing-on-arc--what-the-chain-does-differently) — block time, finality, the decimals trap
- [Worked example](#worked-example--launchpad-to-dex-in-one-join) — launchpad to DEX in one join
- [What differs from upstream](#what-differs-from-upstream)
- [Trap 1 — the network name](#trap-1--the-network-name) · [Trap 2 — AggregatorHook](#trap-2--removing-aggregatorhook)
- [Why the pricing config looks unusual](#why-the-pricing-config-looks-unusual)
- [The two USDC entities](#the-two-usdc-entities) · [Symbols are not identity](#symbols-are-not-identity-on-this-chain)
- [Entities](#entities) · [Build and deploy](#build-and-deploy) · [Verifying a deployment](#verifying-a-deployment)

## The `Hook` entity

V4 mines CREATE2 salts so a hook's **address carries its permissions in the low 14 bits**
(v4-core `Hooks.sol`). A hook's entire capability set is therefore derivable from the address
alone — no ABI, no `eth_call`, no per-vendor data source, and it works on any network with no
`networks.json` entry. This fork decodes that into a first-class entity:

```graphql
{
  hooks(first: 10, orderBy: txCount, orderDirection: desc) {
    id
    poolCount
    txCount
    volumeUSD
    hasCustomAccounting
    beforeSwap
    afterSwap
    beforeSwapReturnsDelta
    afterSwapReturnsDelta
  }
}
```

**Read `hasCustomAccounting` before you trust a number.** It is true when the hook holds any of the
four `*ReturnsDelta` permissions, which let it change the amounts the PoolManager actually settles.
For those pools `volumeUSD` describes what the pool settled, which is not necessarily what the
swapper traded. On Arc that is not a corner case: hooks are near-universal — every top pool has one
— and the busiest hook by transaction count holds both swap return-delta flags.

### Permission flags

Bit positions are from v4-core `Hooks.sol`. The word is the low 14 bits of the hook address, so
`0x…2044` means bits 13, 6 and 2 are set.

| Bit | Field | Bit | Field |
|---:|---|---:|---|
| 13 | `beforeInitialize` | 6 | `afterSwap` |
| 12 | `afterInitialize` | 5 | `beforeDonate` |
| 11 | `beforeAddLiquidity` | 4 | `afterDonate` |
| 10 | `afterAddLiquidity` | 3 | `beforeSwapReturnsDelta` |
| 9 | `beforeRemoveLiquidity` | 2 | `afterSwapReturnsDelta` |
| 8 | `afterRemoveLiquidity` | 1 | `afterAddLiquidityReturnsDelta` |
| 7 | `beforeSwap` | 0 | `afterRemoveLiquidityReturnsDelta` |

`hasCustomAccounting` is `true` when any of bits 3–0 is set.

Decoding is `((addr[18] << 8) | addr[19]) & 0x3fff` on the raw address bytes. Do **not** reach for
`ByteArray.toU32()` / `toI32()`: both `assert(false)` on any nonzero byte past index 3 — which a
20-byte address essentially always has, so the handler aborts — and both read little-endian.
`BigInt.fromUnsignedBytes` carries the same endianness trap.

### What Arc's hook landscape actually looks like

Sampled from the live deployment, 1,000 busiest hooked pools (2026-09-20):

| Flag word | Pools | Permissions |
|---|---:|---|
| `0x2044` | 766 | `beforeInitialize`, `afterSwap`, `afterSwapReturnsDelta` |
| `0x20cc` | 65 | + `beforeSwap`, `beforeSwapReturnsDelta` |
| `0x2acc` | 45 | + `beforeAddLiquidity`, `beforeRemoveLiquidity` |
| `0x05c7` | 33 | `afterAddLiquidity`, `afterRemoveLiquidity`, `beforeSwap`, `afterSwap`, 3 return-deltas |
| `0x00c4` | 17 | `beforeSwap`, `afterSwap`, `afterSwapReturnsDelta` |

852 distinct hook addresses across those 1,000 pools — hooks are per-deployment here, not shared
infrastructure. You can read the flag word straight off the address suffix: everything ending
`…2044` is the same permission set.

**98.6% of those pools — and 98.7% of their transactions — run under a hook that can alter the
amounts settled.** On Arc `hasCustomAccounting` is not an edge case to filter out; it is the
default, and the 1.4% without it are the exception. Treat Arc volume as *settled* amounts unless
you have separately established what a given hook does.

### Hook fields

```
id                   hook contract address, lowercase hex — joins Pool.hooks
permissions          Int!  raw 14-bit flag word
hasCustomAccounting  Boolean!  any *ReturnsDelta bit set
<14 permission booleans, one per bit above>
poolCount            BigInt!   pools initialized with this hook
txCount              BigInt!   swaps + liquidity events across those pools
volumeUSD            BigDecimal!   tracked volume
untrackedVolumeUSD   BigDecimal!
feesUSD              BigDecimal!
totalValueLockedUSD  BigDecimal!   delta-maintained, not recomputed
createdAtTimestamp / createdAtBlockNumber
pools                [Pool!]!  @derivedFrom(field: "hook")
```

`Pool.hook` is nullable by design — `features: [grafting]` is declared and a non-null field
addition is not graft-compatible. `Pool.hooks` (the plain address string) is retained, so existing
queries keep working.

This deliberately departs from the `EulerSwapHook` and `ArrakisHook` entities already in the
schema. Those are vendor-specific, driven by factory events, and require an ABI plus a
`networks.json` entry plus a data source — so they are dead on Arc, which has neither factory.

## Indexing on Arc — what the chain does differently

Facts that change how you consume this data, verified against
[docs.arc.io](https://docs.arc.io/integrate/infrastructure/indexing-events) and Uniswap's
[UniswapX Arc playbook](https://github.com/Uniswap/UniswapX/blob/main/playbook/chains/arc.md).

| | |
|---|---|
| Chain id | `5042` (`0x13b2`); CAIP-2 `eip155:5042`; registry id **`arc`** |
| Block time | ~500 ms, continuous production including empty blocks |
| Finality | Malachite BFT, deterministic. One confirmation is final; **no reorgs** |
| Gas token | **USDC**, not ETH. Constant basefee (20 gwei), so a 250k-gas call costs ~$0.005 |
| Wrapped native | **None exists.** `sdk-core` correctly ships no `WETH9` entry for Arc |
| Indexing rewards | `issuanceRewards: false` in the networks registry |

**Order by block number and log index, never by timestamp.** Arc produces multiple blocks per
second and consecutive blocks can carry an identical `timestamp`, so a timestamp sort is
ambiguous. This subgraph's day/hour buckets are unaffected — integer division into 86400s/3600s
windows is many-to-one by design, and two blocks sharing a timestamp correctly land in the same
bucket — but your own queries should sort on `blockNumber` then `logIndex`.

**The 6-vs-18 decimal trap is the single biggest integration risk on this chain.** Native USDC is
18-decimal; the ERC-20 interface at `0x3600…0000` is 6-decimal over *the same balance*. Anything
reading `eth_getBalance` gets 18-decimal units while every ERC-20 integration speaks 6. See
[The two USDC entities](#the-two-usdc-entities).

## Worked example — launchpad to DEX in one join

Arc's launchpad traffic and its open-market traffic are separate subgraphs that join cleanly on
the token address, and on `Pool.hooks` / `Launch.hook`. Pair this subgraph with
[`argus`](https://thegraph.com/explorer/subgraphs/JBG4rStwjXA3XbD8K4NVJddcjPMGpNgsQPNm3KZdD2GW)
to ask whether a graduated token still trades:

```graphql
# this subgraph — the open-market side
{
  token(id: "0x41358defd0dedc90528b3f1835715e907b686e6a") {
    symbol volumeUSD txCount totalValueLockedUSD
  }
  pools(where: { token1: "0x41358defd0dedc90528b3f1835715e907b686e6a" }) {
    volumeUSD txCount hook { id hasCustomAccounting }
  }
}
```

```graphql
# argus — the launchpad side, same token id
{
  launch(id: "0x41358defd0dedc90528b3f1835715e907b686e6a") {
    symbol bonded swapCount volumeQuote quoteSymbol holderCount hook
  }
}
```

Two cautions that bite here. `Launch.volumeQuote` is denominated in that launch's **quote asset**,
which is USDC for most launches but ARGUS or XAUM for others — mixing them in one ranking is
meaningless, so read `quoteSymbol` before comparing. And the `hook` field on both sides lets you
confirm you are looking at the same pool, which matters on a chain where symbols collide.

## What differs from upstream

Every address below was read from Arc mainnet over JSON-RPC, not taken from documentation.

| Fix | Why it mattered |
|---|---|
| `address(0)` added to `whitelistTokens` | In V4 the native currency is `address(0)`, and on Arc the native currency **is USDC** — so `address(0)` is a dollar, the chain's primary pricing anchor. It was absent, and `getTrackedAmountUSD` gates on that list, so **every native-USDC pool reported `volumeUSD` = exactly 0**. Measured 2026-09-20: `address(0)` held 1,724,991 txs and $521,521 of tracked volume against `0x3600…`'s 4,624,259 txs and $411,575,238 — 37% of the transactions, 0.13% of the volume. Every other chain branch already whitelists its native currency; Arc was the outlier. |
| EURC repointed, USYC dropped | Both previously-listed addresses have **no contract** on Arc mainnet (`eth_getCode` → `0x`). They came from testnet-oriented docs — Uniswap's own [UniswapX Arc playbook](https://github.com/Uniswap/UniswapX/blob/main/playbook/chains/arc.md) warns to "confirm which other stables (EURC, USYC, etc.) are live on Arc mainnet". |
| ARGUS and XAUM whitelisted | Arc's two dominant launchpad quote assets, identified from the Argus protocol's own `quoteAsset` field rather than by symbol. |
| `Token.poolCount` incremented | Initialised upstream and never updated, so every token reported `0` — including USDC across 194k pools. |
| Network name accepts `arc` **and** `arc-mainnet` | See below. The failure mode is silent. |
| `AggregatorHook` data source removed | Tempo-only; the build fails for any other network. |

## Trap 1 — the network name

Three facts that are individually reasonable and collectively a trap:

- Uniswap's `networks.json` calls the chain **`arc-mainnet`**
- The Graph's registrar only accepts **`arc`** — deploying with `arc-mainnet` fails with
  `Specified network is not supported`
- `getSubgraphConfig()` in `chains.ts` matched only `arc-mainnet`, and ends its chain with
  `throw new Error('Unsupported Network')`

So the natural fix for the deploy failure — changing the manifest to `arc` — makes every
`handleInitialize` throw. The result *looks* like a working subgraph: the deploy succeeds, there is
no fatal error, it reports as syncing, the block number advances, and the entity count climbs
because `Transaction` rows come from handlers that never touch the config.

Meanwhile `PoolManager`, `Bundle`, `Token` and `Pool` stay **permanently empty** and every query
returns `indexing_error`. Those four are exactly what `handleInitialize` creates, which is the
signature to look for: **if `transactions` has rows and those four do not, the config threw.**

The fix is one line — accept both names:

```ts
} else if (selectedNetwork == ARC_MAINNET_NETWORK_NAME || selectedNetwork == ARC_NETWORK_NAME) {
```

This belongs upstream, since anyone deploying Arc V4 from that repo hits it.

## Trap 2 — removing AggregatorHook

The committed `subgraph.yaml` is tempo-shaped, and **tempo is the only network in `networks.json`
with an `AggregatorHook` entry**. Building for any other chain fails with
`'AggregatorHook' was not found in the 'arc-mainnet' configuration`.

Careful when removing it: the `PoolManager` data source *references* `AggregatorHook` in its `abis`
list, so a naive text filter deletes PoolManager too — and the build still succeeds, with the main
data source gone. Keep the ABI entry: codegen emits `src/types/PoolManager/AggregatorHook.ts` from
it, and `swap.ts` imports that type.

## Why the pricing config looks unusual

Arc's gas token is USDC and there is no wrapped native — the deployment's `WETH9` slot is an
`UnsupportedProtocol` stub, and `@uniswap/sdk-core` correctly ships no `WETH9` entry for the chain.
So `wrappedNativeAddress` is USDC and `stablecoinWrappedNativePoolId` is `''`, a sentinel that
`getNativePriceInUSD` turns into a price of exactly 1. Confirmed live: `bundles.ethPriceUSD`
returns `1`.

## The two USDC entities

`address(0)` (18 decimals) and `0x3600…0000` (6 decimals) are **the same asset at two precisions**,
not a wrapper and its reserves. Verified directly: one account reports `eth_getBalance` of
`2000000000000000002` and `balanceOf` of `2000000` — exactly 1e12 apart. The subgraph keeps a
`Token` row for each, so "USDC TVL on Arc" read from either address alone is a partial answer.
Sum them.

## Symbols are not identity on this chain

Arc is a memecoin launchpad chain and stablecoin impersonation is routine. Live counts from the
deployment on 2026-09-20:

- **60+ tokens report the symbol `EURC`** — real names include `ExtremelyUglyRichCat` and
  `Extremely Unstable Rectal Coin`
- **19 report `USYC`**
- **At least two report `USDC` with 18 decimals** — their names are `UpSideDownCat` and
  `FatCatBatRatWifHat`
- **Two distinct contracts both call themselves `ARGUS`**, with identical name, symbol, decimals
  and total supply. Only one has a live market.

Never extend a whitelist by symbol. Resolve the address, read `decimals()` on chain, and where a
protocol names its own quote asset, take the protocol's word over the token's.

## Entities

Inherited from upstream unless marked. Full definitions in
[`schema.graphql`](schema.graphql).

| Entity | What it holds |
|---|---|
| **`Hook`** | **New in this fork.** One row per hook address, permissions decoded, activity rolled up |
| `PoolManager` | Chain-level totals — pool count, tx count, volume, TVL |
| `Pool` | Per-pool state, volume, TVL, fee tier, tick spacing, `hooks` + `hook` |
| `Token` | Per-token metadata, volume, TVL, `derivedETH`, `whitelistPools`, `poolCount` |
| `Bundle` | `ethPriceUSD` — pinned to `1` on Arc, since the native currency is a dollar |
| `Swap`, `ModifyLiquidity` | Immutable event rows |
| `Tick`, `Position` | Tick-level liquidity and LP positions |
| `Transaction` | One row per transaction containing indexed events |
| `UniswapDayData`, `PoolDayData`, `PoolHourData`, `TokenDayData`, `TokenHourData` | Time buckets |
| `Subscribe`, `Unsubscribe`, `Transfer` | PositionManager events |
| `EulerSwapHook`, `ArrakisHook` | Vendor-specific, factory-driven — **never written on Arc** |

## Build and deploy

```bash
npm install --legacy-peer-deps
npx graph codegen --output-dir src/types/     # src/types/ is gitignored — this is mandatory
npx graph build
npx graph deploy uniswap-v4---arc --node https://api.studio.thegraph.com/deploy/
```

> **Do not run `yarn generate-subgraph`.** It regenerates `subgraph.yaml` from `networks.json`,
> which keys this chain `arc-mainnet` — rewriting the manifest's `network: arc` into a value the
> registrar rejects. `subgraph.yaml` is hand-maintained in this fork. Edit it directly.

Unit tests (`yarn test`) run under Docker via matchstick and are inherited from upstream. `yarn lint`
is currently broken by an upstream dependency conflict (`@uniswap/eslint-config` against eslint
8.57, `ERR_PACKAGE_PATH_NOT_EXPORTED`) — pre-existing, unrelated to this fork.

### Invariant checks

```bash
python3 scripts/check-invariants.py [endpoint]
```

Checks properties of a **deployed** subgraph against its own indexed data — that every hook's
permissions match its address, that `poolCount` matches the pools observed, that `Hook.volumeUSD`
and `Hook.totalValueLockedUSD` equal the sums over their pools, that no hook holds negative TVL,
and that native-USDC pools price at all.

These exist because unit tests cannot catch aggregate drift. An accumulator maintained by delta is
only wrong after thousands of interleaved events — which is exactly how `Hook.totalValueLockedUSD`
shipped broken in v0.2.0, going *negative*, and was caught here rather than in review. Stdlib and
curl only; exits non-zero on failure.

## Verifying a deployment

Do this before believing any number:

```graphql
{ _meta { block { number } hasIndexingErrors } }
```

`hasIndexingErrors` must be `false`. Then confirm the config-dependent entities exist — per Trap 1,
their absence alongside populated `transactions` is the signature of a config that threw:

```graphql
{
  poolManagers(first: 1) { poolCount txCount totalVolumeUSD }
  bundles(first: 1) { ethPriceUSD }
  hooks(first: 3, orderBy: txCount, orderDirection: desc) { id poolCount hasCustomAccounting }
  pools(first: 5, orderBy: volumeUSD, orderDirection: desc) {
    volumeUSD totalValueLockedUSD txCount
    token0 { symbol decimals } token1 { symbol decimals }
  }
}
```

Rank pools by `volumeUSD`, never by TVL — a TVL sort on Uniswap-schema subgraphs surfaces dead
pools with zero volume at the top. And sanity-check volume per transaction: on a chain this noisy,
a pool reporting six-figure average notional across double-digit transactions is a pricing
artifact, not a whale.

## Not yet applied

`Bytes` as entity IDs. All entities still use `id: ID!`. Immutability is already correct upstream —
6 entities are `immutable: true`, and they are the right ones. The Bytes conversion is worth more on
V4 than most chains, since pools are keyed by `bytes32` and positions by token id, so those IDs are
byte-shaped already and stored as hex strings.

## Licence

GPL-3.0, inherited from [Uniswap/v4-subgraph](https://github.com/Uniswap/v4-subgraph).
