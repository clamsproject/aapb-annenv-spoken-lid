"""
Samples the beginning and the end of each video in full, and n clips of the
rest at random, and stores the samples in the annotation database (see
sampling.py).

A clip belongs to the beginning when its midpoint is in the first
`--head-ms` of the video, and to the end when its midpoint is in the last
`--tail-ms`. The duration of the video comes from its silencedetect log.
"""
import sampling


def add_options(p):
    p.add_argument("--head-ms", type=int, default=180000,
                   help="length of the beginning (default: 180000, 3 min)")
    p.add_argument("--tail-ms", type=int, default=300000,
                   help="length of the end (default: 300000, 5 min)")


def choose(video, args, rng):
    end_boundary = video.duration_ms() - args.tail_ms
    beginning, middle, ending = [], [], []
    for clip in video.clips:
        t = sampling.midpoint_ms(clip)
        if t < args.head_ms:
            beginning.append(clip)
        elif t < end_boundary:
            middle.append(clip)
        else:
            ending.append(clip)
    picked = rng.sample(middle, min(args.n, len(middle)))
    return ([(c, "beginning") for c in beginning]
            + [(c, "middle") for c in picked]
            + [(c, "ending") for c in ending])


if __name__ == "__main__":
    sampling.main(__doc__, "convention", choose, add_options,
                  lambda args: {"head_ms": args.head_ms,
                                "tail_ms": args.tail_ms})
