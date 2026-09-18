# Uniswap V4 on Arc — subgraph

Fork of [Uniswap/v4-subgraph](https://github.com/Uniswap/v4-subgraph) (GPL-3.0) deployed for
**Arc Mainnet** (`eip155:5042`), Circle's USDC-native L1.

Live: `https://api.studio.thegraph.com/query/111767/uniswap-v4---arc/v0.1.1`

| | |
|---|---|
| PoolManager | `0x8366a39cc670b4001a1121b8f6a443a643e40951` |
| PositionManager | `0x6049c9a0e26405c0985f9e3685c87d0ae917f82b` |
| Start block | 1948011 |

Uniswap already ships the Arc config upstream — `networks.json` has both contracts, and `chains.ts`
has the pricing setup. **Two changes were needed to actually make it index**, both recorded below
because neither is obvious and the failure mode of the first is silent.

## 1. The network name (this is the important one)

Three facts that are individually reasonable and collectively a trap:

- Uniswap's `networks.json` calls the chain **`arc-mainnet`**
- The Graph's registrar only accepts **`arc`** — deploying with `arc-mainnet` fails with
  `Specified network is not supported`
- `getSubgraphConfig()` in `chains.ts` matches on `arc-mainnet` and ends its chain with
  `throw new Error('Unsupported Network')`

So the natural fix for the deploy failure — changing the manifest to `arc` — makes every
`handleInitialize` throw. The result looks like a working subgraph:

- deploy succeeds
- no *fatal* error; the subgraph reports as syncing and the block number advances
- entity count climbs, because `Transaction` rows come from handlers that never hit the config

...while `PoolManager`, `Bundle`, `Token` and `Pool` stay **permanently empty**, and every query
returns `indexing_error`. Those four are exactly what `handleInitialize` creates, which is the
signature to look for: if `transactions` has rows and those four don't, the config threw.

The fix is one line — accept both names:

```ts
} else if (selectedNetwork == ARC_MAINNET_NETWORK_NAME || selectedNetwork == ARC_NETWORK_NAME) {
```

This belongs upstream, since anyone deploying Arc V4 from that repo hits it.

## 2. AggregatorHook

The committed `subgraph.yaml` is tempo-shaped, and **tempo is the only network in `networks.json`
with an `AggregatorHook` entry**. Building for any other chain fails with
`'AggregatorHook' was not found in the 'arc-mainnet' configuration`. Removed that data source here.

Careful when removing it: the `PoolManager` data source *references* `AggregatorHook` in its `abis`
list, so a naive text filter deletes PoolManager too and the build still succeeds — with the main
data source gone.

## Why the pricing config looks unusual (upstream's work, not ours)

Arc's gas token is USDC and there is no wrapped native — the deployment's `WETH9` slot is an
`UnsupportedProtocol` stub. So `wrappedNativeAddress` is USDC and
`stablecoinWrappedNativePoolId` is `''`, a sentinel that `getNativePriceInUSD` turns into a price of
exactly 1. Confirmed live: `bundles.ethPriceUSD` returns `1`.

The subtlety worth knowing: native USDC is 18-decimal while the USDC ERC-20 at
`0x3600000000000000000000000000000000000000` is 6-decimal. Both resolve correctly — verified by
querying token decimals from the live subgraph.

Whitelist is USDC, EURC, USYC, CIRBTC and bridged WETH.

## Verifying a deployment

Do this before believing any of the numbers — it is the step that would have caught the bug above
immediately:

```graphql
{ _meta { block { number } hasIndexingErrors } }
```

`hasIndexingErrors` must be `false`. Then check that the config-dependent entities actually exist:

```graphql
{
  poolManagers(first: 1) { id poolCount txCount }
  bundles(first: 1) { ethPriceUSD }
  pools(first: 5, orderBy: volumeUSD, orderDirection: desc) {
    id volumeUSD totalValueLockedUSD txCount
    token0 { symbol decimals }
    token1 { symbol decimals }
  }
}
```

Rank pools by `volumeUSD`, not TVL — a TVL sort on Uniswap-schema subgraphs surfaces dead pools
with zero volume at the top.

## Build

```bash
npm install --legacy-peer-deps
npx graph codegen --output-dir src/types/
npx graph build --network arc-mainnet   # networks.json key
# then set `network: arc` in the manifest before deploying — see above
npx graph deploy uniswap-v4---arc
```

## Not yet applied

`Bytes` as entity IDs. All 19 entities still use `id: ID!`. Immutability is already correct
upstream — 6 entities are `immutable: true`, and they are the right ones. The Bytes conversion is
worth more on V4 than most chains, since pools are keyed by `bytes32` and positions by token id, so
those IDs are byte-shaped already and stored as hex strings.
