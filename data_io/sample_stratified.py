"""
Samples n clips of each video, spread over its duration, and stores the
samples in the annotation database (see sampling.py).

The clips, in the order of their midpoints, are divided into strata with
equal numbers of clips, and the same number of clips is chosen at random from
each stratum (the first strata get one more when n is not a multiple of the
number of strata).
"""
import sampling


def add_options(p):
    p.add_argument("--strata", type=int, default=5,
                   help="number of strata (default: 5)")


def choose(video, args, rng):
    ordered = sorted(video.clips, key=sampling.midpoint_ms)
    n = min(args.n, len(ordered))
    k = args.strata
    strata = [ordered[i * len(ordered) // k:(i + 1) * len(ordered) // k]
              for i in range(k)]
    quotas = [n // k + (1 if i < n % k else 0) for i in range(k)]
    picked = []
    for i, (stratum, quota) in enumerate(zip(strata, quotas), start=1):
        chosen = rng.sample(stratum, min(quota, len(stratum)))
        picked.extend((clip, f"time_bin_{i}") for clip in chosen)
    return picked


if __name__ == "__main__":
    sampling.main(__doc__, "stratified", choose, add_options,
                  lambda args: {"strata": args.strata})
