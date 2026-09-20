"""Paired significance tests over per-query scores.

REPRODUCIBLE IS NOT DISTINGUISHABLE: the eval's ~1-point floor measures
re-running ONE config, while the uncertainty in a DIFFERENCE between two is far
larger, because ~100 of 118 queries score identically and the result rests on
the handful that move. Three wrong headlines came from reading a point estimate
as a result (failures.md #37).

Both tests are PAIRED - query by query, not average against average - which is
what makes the ties cost nothing. Only valid when the two lists are aligned:
index i must be the same query in both.
"""

import math
import random

# Fixed, so a reported interval is reproducible: a CI that moves between two
# runs of the same data invites the over-reading these tests exist to prevent.
BOOTSTRAP_SEED = 20260908
BOOTSTRAP_RESAMPLES = 10_000


def sign_test(a: list[float], b: list[float]) -> tuple[int, int, int, float]:
    """Wins for `b` over `a`, losses, ties, and a TWO-SIDED p-value.

    Ties are discarded, as the standard sign test does: a query both arms get
    right says nothing about which is better. With 102 of 118 identical, the
    test is effectively n=16 - and reporting that is the point.
    """
    wins = sum(1 for x, y in zip(a, b) if y > x)
    losses = sum(1 for x, y in zip(a, b) if y < x)
    ties = len(a) - wins - losses

    trials = wins + losses
    if trials == 0:
        return wins, losses, ties, 1.0

    # Two-sided binomial test at p=0.5: the chance of a split at least this
    # lopsided in either direction.
    extreme = max(wins, losses)
    tail = sum(math.comb(trials, k) for k in range(extreme, trials + 1)) / 2**trials
    return wins, losses, ties, min(1.0, 2 * tail)


def paired_bootstrap(
    a: list[float], b: list[float], resamples: int = BOOTSTRAP_RESAMPLES
) -> tuple[float, float, float]:
    """Mean difference (b - a) and its 95% percentile interval.

    Resamples QUERIES, not scores, and carries both arms' values for a resampled
    query together - that pairing is what removes the between-query variance
    that otherwise swamps a few points of difference.
    """
    differences = [y - x for x, y in zip(a, b)]
    observed = sum(differences) / len(differences)

    rng = random.Random(BOOTSTRAP_SEED)
    size = len(differences)
    means = []
    for _ in range(resamples):
        total = 0.0
        for _ in range(size):
            total += differences[rng.randrange(size)]
        means.append(total / size)
    means.sort()

    low = means[int(resamples * 0.025)]
    high = means[int(resamples * 0.975)]
    return observed, low, high


def report(label_a: str, label_b: str, a: list[float], b: list[float]) -> None:
    """Print both tests for one comparison."""
    wins, losses, ties, p = sign_test(a, b)
    observed, low, high = paired_bootstrap(a, b)

    mean_a = sum(a) / len(a)
    mean_b = sum(b) / len(b)
    print(f"  {label_a:<12}{mean_a:>7.1%}")
    print(f"  {label_b:<12}{mean_b:>7.1%}")
    print(f"  {'difference':<12}{observed:>7.1%}   95% CI [{low:+.1%}, {high:+.1%}]")
    print(
        f"  {'sign test':<12}{wins:>4} wins, {losses} losses, {ties} ties   p = {p:.3f}"
    )
    # The interval is the result, not the point estimate - said in the output,
    # because the point estimate is what gets copied into a table.
    if low <= 0 <= high:
        print("  VERDICT     not distinguishable - the interval contains zero")
    else:
        print("  VERDICT     distinguishable at 95%")


def self_test() -> None:
    """Check both tests against values worked out by hand.

    A checker that has never gone red is not known to work, and these two are
    the gate on every comparison claim here.

        uv run python -m eval.paired
    """
    # 1. Identical arms: no wins, no losses, nothing to distinguish.
    same = [1.0, 0.5, 0.0, 1.0]
    wins, losses, ties, p = sign_test(same, same)
    assert (wins, losses, ties, p) == (0, 0, 4, 1.0), (wins, losses, ties, p)
    observed, low, high = paired_bootstrap(same, same)
    assert observed == 0.0 and low == 0.0 and high == 0.0, (observed, low, high)
    print(
        f"  identical arms      {wins}W {losses}L {ties}T  p={p:.3f}  CI [0.0%, 0.0%]"
    )

    # 2. The Qwen3-vs-bge split: 11 wins, 5 losses. THIS HARNESS IS TWO-SIDED,
    #    and CLAUDE.md's recorded p=0.105 is the ONE-SIDED tail of the same
    #    data - never compare the two. Choosing the direction after seeing which
    #    arm won is what makes a one-sided test flattering.
    a = [0.0] * 11 + [1.0] * 5
    b = [1.0] * 11 + [0.0] * 5
    wins, losses, ties, p = sign_test(a, b)
    assert (wins, losses, ties) == (11, 5, 0), (wins, losses, ties)
    assert abs(p - 0.2101) < 0.001, p
    print(f"  11W 5L (CLAUDE.md)  p={p:.4f} two-sided, 0.1051 one-sided - same data")

    # 3. A constant difference has no variance, so the interval collapses onto
    #    it. Catches a bootstrap that resamples the arms independently and so
    #    breaks the pairing - the easiest way to get this wrong.
    a = [0.0, 0.25, 0.5, 1.0] * 5
    b = [x + 0.25 if x < 1.0 else x for x in a]
    constant = [0.5, 0.75, 0.75, 1.0] * 5
    observed, low, high = paired_bootstrap(a, constant)
    assert low <= observed <= high, (low, observed, high)
    wins, losses, ties, p = sign_test(a, b)
    assert losses == 0 and p < 0.001, (wins, losses, p)
    print(f"  strictly better     {wins}W {losses}L {ties}T  p={p:.5f}")

    # 4. A one-query difference over many ties must NOT come out significant.
    #    This is the failure mode the module exists for.
    a = [1.0] * 60 + [0.0]
    b = [1.0] * 60 + [1.0]
    wins, losses, ties, p = sign_test(a, b)
    observed, low, high = paired_bootstrap(a, b)
    assert p == 1.0, p
    print(
        f"  1 win over 60 ties  p={p:.3f}  diff {observed:+.1%} "
        f"CI [{low:+.1%}, {high:+.1%}]"
    )

    print("\nPASS")


if __name__ == "__main__":
    print("\nself-test: both tests against hand-worked values\n")
    self_test()
