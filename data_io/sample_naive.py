"""
Samples n clips of each video at random, and stores the samples in the
annotation database (see sampling.py).
"""
import sampling


def choose(video, args, rng):
    picked = rng.sample(video.clips, min(args.n, len(video.clips)))
    return [(clip, None) for clip in picked]


if __name__ == "__main__":
    sampling.main(__doc__, "naive", choose)
