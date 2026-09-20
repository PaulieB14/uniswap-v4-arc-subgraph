#!/usr/bin/env python3
"""
Integration checks against a deployed Uniswap-V4-on-Arc subgraph.

These are invariants the mappings must hold, checked against real indexed data. They exist
because matchstick unit tests do not catch aggregate drift: an accumulator can be wrong only
after thousands of interleaved events, which is exactly how Hook.totalValueLockedUSD shipped
broken in v0.2.0 (it went NEGATIVE) and was caught here rather than in review.

    python3 scripts/check-invariants.py [endpoint]

Exits non-zero on any failure. No dependencies beyond the standard library and curl.
"""
import collections
import json
import subprocess
import sys

DEFAULT_EP = "https://api.studio.thegraph.com/query/111767/uniswap-v4---arc/v0.2.1"
ZERO = "0x0000000000000000000000000000000000000000"
ALL_HOOK_MASK = 0x3FFF
RETURNS_DELTA_MASK = 0b1111

# bit position -> schema field, from v4-core Hooks.sol
FLAGS = [
    (13, "beforeInitialize"), (12, "afterInitialize"),
    (11, "beforeAddLiquidity"), (10, "afterAddLiquidity"),
    (9, "beforeRemoveLiquidity"), (8, "afterRemoveLiquidity"),
    (7, "beforeSwap"), (6, "afterSwap"),
    (5, "beforeDonate"), (4, "afterDonate"),
    (3, "beforeSwapReturnsDelta"), (2, "afterSwapReturnsDelta"),
    (1, "afterAddLiquidityReturnsDelta"), (0, "afterRemoveLiquidityReturnsDelta"),
]

HOOK_FIELDS = (
    "id permissions hasCustomAccounting poolCount txCount volumeUSD feesUSD "
    "totalValueLockedUSD " + " ".join(name for _, name in FLAGS)
)
POOL_FIELDS = "id hooks volumeUSD txCount totalValueLockedUSD hook{id} token0{id} token1{id}"


def gql(endpoint, query, tries=3):
    for _ in range(tries):
        proc = subprocess.run(
            ["curl", "-s", "--max-time", "90", "-X", "POST", endpoint,
             "-H", "content-type: application/json",
             "-d", json.dumps({"query": query})],
            capture_output=True, text=True,
        )
        try:
            body = json.loads(proc.stdout)
        except ValueError:
            continue
        if "errors" in body:
            print("  graphql error:", str(body["errors"])[:200])
            continue
        return body["data"]
    raise SystemExit("query failed after retries")


def page(endpoint, entity, fields):
    """Page an entity by id, around graph-node's 1000-row ceiling."""
    rows, cursor = [], ""
    while True:
        data = gql(endpoint, '{ %s(first:1000, where:{id_gt:"%s"}, orderBy:id, orderDirection:asc){ %s } }'
                   % (entity, cursor, fields))
        batch = data[entity]
        if not batch:
            break
        rows += batch
        cursor = batch[-1]["id"]
        if len(batch) < 1000:
            break
    return rows


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def main():
    endpoint = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EP
    failures = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else "\n       " + detail))
        if not ok:
            failures.append(name)

    meta = gql(endpoint, "{ _meta{block{number} hasIndexingErrors} "
                         "poolManagers{poolCount txCount totalVolumeUSD totalValueLockedUSD} }")
    block = meta["_meta"]["block"]["number"]
    manager = meta["poolManagers"][0]
    print(f"=== {endpoint}")
    print(f"=== block {block:,}  indexingErrors={meta['_meta']['hasIndexingErrors']}")
    print(f"=== pools {int(manager['poolCount']):,}  txs {int(manager['txCount']):,}  "
          f"volume ${float(manager['totalVolumeUSD']):,.2f}\n")

    check("indexing is error-free", meta["_meta"]["hasIndexingErrors"] is False)

    hooks = page(endpoint, "hooks", HOOK_FIELDS)
    pools = page(endpoint, "pools", POOL_FIELDS)
    print(f"fetched {len(hooks)} hooks, {len(pools)} pools\n")

    # Permissions are derived from the address, so they can be recomputed independently.
    bad, example = 0, ""
    for hook in hooks:
        want = int(hook["id"], 16) & ALL_HOOK_MASK
        if int(hook["permissions"]) != want:
            bad += 1
            example = f"{hook['id']} permissions={hook['permissions']} expected={want}"
            continue
        mismatch = next((n for b, n in FLAGS if bool(hook[n]) != bool(want & (1 << b))), None)
        if mismatch:
            bad += 1
            example = f"{hook['id']} {mismatch} disagrees with bit"
        elif bool(hook["hasCustomAccounting"]) != bool(want & RETURNS_DELTA_MASK):
            bad += 1
            example = f"{hook['id']} hasCustomAccounting disagrees with return-delta bits"
    check(f"hook permissions match their address ({len(hooks)} hooks)", bad == 0,
          f"{bad} wrong, e.g. {example}")

    observed = collections.Counter(p["hooks"] for p in pools)
    wrong = [h["id"] for h in hooks if int(h["poolCount"]) != observed.get(h["id"], 0)]
    check("Hook.poolCount equals its observed pools", not wrong,
          f"{len(wrong)} wrong, e.g. {wrong[:3]}")

    unlinked = [p["id"] for p in pools if (p.get("hook") or {}).get("id") != p["hooks"]]
    check("Pool.hook resolves to Pool.hooks", not unlinked, f"{len(unlinked)} mismatched")

    volume, tvl = collections.defaultdict(float), collections.defaultdict(float)
    for p in pools:
        volume[p["hooks"]] += float(p["volumeUSD"])
        tvl[p["hooks"]] += float(p["totalValueLockedUSD"])

    off = [(h["id"], float(h["volumeUSD"]), volume.get(h["id"], 0.0))
           for h in hooks if not approx(float(h["volumeUSD"]), volume.get(h["id"], 0.0))]
    check("Hook.volumeUSD equals the sum over its pools", not off,
          f"{len(off)} off, e.g. {[(a, round(b, 2), round(c, 2)) for a, b, c in off[:3]]}")

    # The accumulator that shipped broken: maintained by delta, so every write to a pool's TVL
    # must be bracketed by a before/after pair in the same handler or this drifts, and can go
    # negative. Looser tolerance than volume because TVL is repriced, not only accumulated.
    off = [(h["id"], float(h["totalValueLockedUSD"]), tvl.get(h["id"], 0.0))
           for h in hooks if not approx(float(h["totalValueLockedUSD"]), tvl.get(h["id"], 0.0), 1e-4)]
    check("Hook.totalValueLockedUSD equals the sum over its pools", not off,
          f"{len(off)} off, e.g. {[(a, round(b, 2), round(c, 2)) for a, b, c in off[:3]]}")

    negative = [h["id"] for h in hooks if float(h["totalValueLockedUSD"]) < 0]
    check("no Hook holds negative TVL", not negative, f"{len(negative)} negative, e.g. {negative[:3]}")

    check("sum(Hook.volumeUSD) equals poolManager.totalVolumeUSD",
          approx(sum(float(h["volumeUSD"]) for h in hooks), float(manager["totalVolumeUSD"])),
          f"hooks={sum(float(h['volumeUSD']) for h in hooks):,.2f} "
          f"manager={float(manager['totalVolumeUSD']):,.2f}")

    check("sum(Hook.poolCount) equals poolManager.poolCount",
          sum(int(h["poolCount"]) for h in hooks) == int(manager["poolCount"]),
          f"hooks={sum(int(h['poolCount']) for h in hooks)} manager={manager['poolCount']}")

    # The whitelist fix: address(0) is the native currency and on Arc that is USDC, so pools
    # against it must price. Before the fix every one of these reported exactly zero.
    native = [p for p in pools if ZERO in (p["token0"]["id"], p["token1"]["id"])]
    priced = [p for p in native if float(p["volumeUSD"]) > 0]
    check(f"native-USDC pools carry tracked volume ({len(priced)}/{len(native)})",
          not native or priced, "none priced — address(0) is missing from whitelistTokens")

    custom = sum(1 for h in hooks if h["hasCustomAccounting"])
    print(f"\n{custom}/{len(hooks)} hooks can alter settled amounts (hasCustomAccounting)")
    print(f"\n{len(failures)} failure(s): {failures}" if failures else "\nALL CHECKS PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
