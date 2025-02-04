#!/usr/bin/env python3

"""Convert a video into a set of files to play it using HLS.

The video will be converted to different resolutions, using different
bitrates. A master playlist will be generated to be processed by an
HLS client. A progressive MP4 version is also produced (to be used as
a fallback), as well as a poster image.

There are many options, but not all of them are safe to change. For
example, HLS is usually expecting AAC-LC as a codec for audio. This
can be changed, but this may not work in all browsers.

One important option is ``--hls-type``. Choosing ``fmp4`` is more
efficient but is only compatible with iOS 10+.

Most video options take several parameters (space-separated). When all
options do not have the same length, the length of ``--video-widths`` is
used to normalize all lengths. Last value is repeated if needed.

The ``--video-overlay`` enables to overlay a text with technical
information about the video on each video. The value is a pattern like
``{resolution}p``. The allowed variables in pattern are the ones specified
as a video option. Same applies for ``--mp4-overlay``.

The audio options are global as audio need to be switched seamlessly
between segments and it is not possible when using different bitrates
or options. The ``--audio-only`` option is like adding a video width
of 0 at the end of the video width list. The ``--audio-separate`` will
encode the audio track separately from the video (less bandwidth).

The default output directory is the basename of the input video.

"""

import argparse
import json
import logging
import logging.handlers
import math
import operator
import os
import re
import shlex
import subprocess
import sys


logger = logging.getLogger("video2hls")


class CustomFormatter(
    argparse.RawDescriptionHelpFormatter, argparse.ArgumentDefaultsHelpFormatter
):
    pass


def parse_args(fake_args=None):
    """Parse arguments."""
    parser = argparse.ArgumentParser(
        description=sys.modules[__name__].__doc__, formatter_class=CustomFormatter
    )

    g = parser.add_mutually_exclusive_group()
    g.add_argument(
        "--debug", "-d", action="store_true", default=False, help="enable debugging"
    )
    g.add_argument(
        "--silent",
        "-s",
        action="store_true",
        default=False,
        help="don't log to console",
    )

    g = parser.add_argument_group("hls options")
    g.add_argument(
        "--hls-type",
        metavar="TYPE",
        default="mpegts",
        choices=("mpegts", "fmp4"),
        help="HLS segment type",
    )
    g.add_argument(
        "--hls-time",
        metavar="DURATION",
        default=6,
        type=int,
        help="HLS segment duration (in seconds)",
    )
    g.add_argument(
        "--hls-segments",
        metavar="FILENAMES",
        default="{resolution}p_{index}",
        help="pattern to use for HLS segment files",
    )
    g.add_argument(
        "--hls-segment-prefix",
        metavar="PREFIX",
        default="",
        type=str,
        help="prefix to use for segments in media playlists",
    )
    g.add_argument(
        "--hls-playlist-prefix",
        metavar="PREFIX",
        default=[],
        type=str,
        nargs="+",
        help="prefix to use for playlists in master playlist",
    )
    g.add_argument(
        "--hls-master-playlist",
        metavar="NAME",
        default="index.m3u8",
        type=str,
        help="master playlist name",
    )
    g.add_argument(
        "--hls-no-codecs",
        action="store_false",
        default=True,
        dest="hls_add_codecs",
        help="do not compute codecs for master playlist",
    )

    g = parser.add_argument_group("video options")
    g.add_argument(
        "--video-widths",
        metavar="WIDTH",
        default=[3840, 2560, 1920, 1280, 854, 640, 428],
        nargs="+",
        type=int,
        help="video resolutions (width in pixels)",
    )
    g.add_argument(
        "--video-bitrates",
        metavar="RATE",
        default=[14000, 6500, 4500, 2500, 1300, 800, 400],
        nargs="+",
        type=int,
        help="video bitrates (in kbits/s)",
    )
    g.add_argument(
        "--video-codecs",
        metavar="CODEC",
        default=["h264"],
        nargs="+",
        type=str,
        help="video codecs",
    )
    g.add_argument(
        "--video-profiles",
        metavar="PROFILE",
        default=["high@5.1", "high@5.1", "main@3.2", "main@3.1"],
        nargs="+",
        type=str,
        help="video profile (name@level)",
    )
    g.add_argument(
        "--video-names",
        metavar="NAME",
        default=[],
        nargs="+",
        type=str,
        help="video name (used in playlists)",
    )
    g.add_argument(
        "--video-overlay",
        metavar="TEXT",
        type=str,
        help="add an overlay with technical info about the video",
    )
    g.add_argument(
        "--video-bitrate-factor",
        type=float,
        default=1.0,
        help="factor to apply to provided bitrates",
    )
    g.add_argument(
        "--video-presets",
        metavar="PRESET",
        default=[],
        nargs="+",
        type=str,
        help="video presets",
    )

    g = parser.add_argument_group("audio options")
    g.add_argument(
        "--no-audio",
        action="store_false",
        default=True,
        dest="audio",
        help="remove audio track",
    )
    g.add_argument(
        "--audio-sampling", metavar="RATE", type=int, help="audio sampling rate"
    )
    g.add_argument(
        "--audio-bitrate",
        metavar="RATE",
        default=96,
        type=int,
        help="audio bitrate (in kbits)",
    )
    g.add_argument("--audio-codec", metavar="CODEC", default="aac", help="audio codec")
    g.add_argument(
        "--audio-profile", metavar="PROFILE", default="aac_low", help="audio profile"
    )
    g.add_argument(
        "--audio-only",
        action="store_true",
        default=False,
        help="also generate an audio-only variant",
    )
    g.add_argument(
        "--audio-separate",
        action="store_true",
        default=False,
        help="keep audio track in separate media playlist",
    )

    g = parser.add_argument_group("progressive MP4 options")
    g.add_argument(
        "--no-mp4",
        action="store_false",
        default=True,
        dest="mp4",
        help="disable progressive MP4 version",
    )
    g.add_argument(
        "--mp4-width",
        metavar="WIDTH",
        type=int,
        help="progressive MP4 width (in pixels)",
    )
    g.add_argument(
        "--mp4-max-width",
        metavar="WIDTH",
        type=int,
        default=1280,
        help="progressive MP4 maximum width (in pixels)",
    )
    g.add_argument(
        "--mp4-bitrate-factor",
        metavar="RATE",
        type=float,
        default=0.8,
        help="progressive MP4 bitrate factor",
    )
    g.add_argument(
        "--mp4-bitrate",
        metavar="RATE",
        type=int,
        help="progressive MP4 bitrate (in kbits/s)",
    )
    g.add_argument(
        "--mp4-codec",
        metavar="CODEC",
        type=str,
        default="h264",
        help="progressive MP4 codec",
    )
    g.add_argument(
        "--mp4-profile",
        metavar="PROFILE",
        type=str,
        default="main@3.1",
        help="progressive MP4 profile (name@level)",
    )
    g.add_argument(
        "--mp4-overlay",
        metavar="TEXT",
        type=str,
        help="add an overlay with technical info about the video",
    )
    g.add_argument(
        "--mp4-filename",
        metavar="NAME",
        type=str,
        default="progressive.mp4",
        help="filename for progressive MP4",
    )
    g.add_argument(
        "--mp4-preset", metavar="PRESET", type=str, help="progressive MP4 preset"
    )

    g = parser.add_argument_group("poster option")
    g.add_argument(
        "--no-poster",
        action="store_false",
        default=True,
        dest="poster",
        help="disable poster image",
    )
    g.add_argument(
        "--poster-quality",
        metavar="Q",
        default=10,
        type=int,
        help="poster quality (from 0 to 100)",
    )
    g.add_argument(
        "--poster-grayscale",
        default=False,
        action="store_true",
        help="convert poster to grayscale",
    )
    g.add_argument(
        "--poster-filename",
        metavar="FILE",
        default="poster.jpg",
        type=str,
        help="poster filename",
    )
    g.add_argument(
        "--poster-seek",
        metavar="POS",
        default="5%",
        help="seek to the given position (5%% or 15s)",
    )
    g.add_argument(
        "--poster-width", metavar="WIDTH", type=int, help="poster width (in pixels)"
    )
    g.add_argument(
        "--poster-max-width",
        metavar="WIDTH",
        default=1280,
        type=int,
        help="poster maximum width (in pixels)",
    )

    g = parser.add_argument_group("program options")
    g.add_argument(
        "--ffmpeg", metavar="EXE", default="ffmpeg", help="ffmpeg executable name"
    )
    g.add_argument(
        "--ffprobe", metavar="EXE", default="ffprobe", help="ffprobe executable name"
    )
    g.add_argument(
        "--mp4file", metavar="EXE", default="mp4file", help="mp4file executable name"
    )

    parser.add_argument(
        "--ratio", metavar="RATIO", default="16:9", help="video ratio (not enforced)"
    )
    parser.add_argument("--output", metavar="DIR", help="output directory")
    parser.add_argument(
        "--output-overwrite",
        action="store_true",
        default=False,
        help="overwrite output directory if it exists",
    )
    parser.add_argument("input", metavar="VIDEO", help="video to be converted")

    options = parser.parse_args(fake_args)

    # Handle output directory
    if options.output is None:
        options.output = os.path.splitext(options.input)[0]
        if options.output == options.input:
            options.output += "_output"
    options.output = os.path.abspath(options.output)

    return options


class ColorizingStreamHandler(logging.StreamHandler):
    """Provide a nicer logging output to error output with colors."""

    color_map = dict(
        [
            (x, i)
            for i, x in enumerate(
                "black red green yellow blue " "magenta cyan white".split(" ")
            )
        ]
    )
    level_map = {
        logging.DEBUG: (None, "blue", " DBG"),
        logging.INFO: (None, "green", "INFO"),
        logging.WARNING: (None, "yellow", "WARN"),
        logging.ERROR: (None, "red", " ERR"),
        logging.CRITICAL: ("red", "white", "CRIT"),
    }
    csi = "\x1b["
    reset = "\x1b[0m"

    @property
    def is_tty(self):
        isatty = getattr(self.stream, "isatty", None)
        return isatty and isatty()

    def format(self, record):
        message = logging.StreamHandler.format(self, record)
        params = []
        levelno = record.levelno
        if levelno not in self.level_map:
            levelno = logging.WARNING
        bg, fg, level = self.level_map[levelno]
        if bg in self.color_map:
            params.append(str(self.color_map[bg] + 40))
        if fg in self.color_map:
            params.append(str(self.color_map[fg] + 30))
        params.append("1m")
        level = "[{}]".format(level)
        return "\n".join(
            [
                "{}: {}".format(
                    self.is_tty
                    and params
                    and "".join((self.csi, ";".join(params), level, self.reset))
                    or level,
                    line,
                )
                for line in message.split("\n")
            ]
        )


def setup_logging(options):
    """Configure logging."""
    root = logging.getLogger("")
    root.setLevel(logging.WARNING)
    logger.setLevel(options.debug and logging.DEBUG or logging.INFO)
    if not options.silent:
        root.addHandler(ColorizingStreamHandler())


def run(options, what, *args):
    """Execute either ffmpeg or ffprobe with the provided arguments.

    Comments are filtered out, except when displaying to user.

    """
    if what in {"ffprobe", "ffmpeg", "mp4file"}:
        what = getattr(options, what)

    # Pretty format arguments
    jargs = []
    for arg in args:
        if arg.startswith("# "):
            jargs.append(f"`{arg}`")
        else:
            if not jargs or jargs[-1].startswith("`"):
                jargs.append(f" {shlex.quote(arg)}")
            else:
                jargs[-1] += f" {shlex.quote(arg)}"
    if jargs and jargs[0].startswith("`"):
        jargs.insert(0, "")
    jargs = " \\\n   ".join(jargs)
    logger.debug(f"execute {what} {jargs}")

    proc = subprocess.Popen(
        [what] + [arg for arg in args if not arg.startswith("# ")],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = proc.communicate(None)
    stdout = stdout.decode("utf-8", "replace")
    stderr = stderr.decode("utf-8", "replace")

    if proc.returncode != 0:
        logger.error(
            "{} error:\n{}\n{}\n{}".format(
                what,
                f" A: {jargs}",
                "\n".join(
                    [" O: {}".format(line) for line in stdout.rstrip().split("\n")]
                ),
                "\n".join(
                    [" E: {}".format(line) for line in stderr.rstrip().split("\n")]
                ),
            )
        )
        raise RuntimeError(
            f"Unable to execute {what}. Return code {proc.returncode}.\n"
            f"\nStdout: {stdout}"
            f"\n\nStderr: {stderr}"
        )
    return stdout


def probe(options):
    """Probe input file to extract technical information.

    We only keep the first video stream and the first audio
    stream. This may not be the "best" streams ffmpeg would select.

    """
    logger.info(f"probe {options.input}")
    out = run(
        options,
        "ffprobe",
        "# don't display any status message",
        "-v",
        "quiet",
        "# JSON output",
        "-print_format",
        "json",
        "# get stream information",
        "-show_entries",
        "format=duration:streams",
        "# input video",
        options.input,
    )
    info = json.loads(out)
    streams = info["streams"]

    # Only keep first audio and first video
    result = {
        "video": ([x for x in streams if x["codec_type"] == "video"] or [None])[0],
        "audio": ([x for x in streams if x["codec_type"] == "audio"] or [None])[0],
    }
    if "duration" not in result["video"]:
        result["video"]["duration"] = info["format"]["duration"]
    return result


def extract_codecs(options, sample):
    """Extract codecs information from the given MP4 sample.

    This makes use of mp4file since extracting this information is
    quite complex. This is not complete as all codecs are a bit
    different. See RFC 6381, section 3.3 and this SO question:
    https://stackoverflow.com/questions/16363167/html5-video-tag-codecs-attribute#16365526

    """
    result = run(options, "mp4file", "--dump", sample)
    result = result.split("\n")
    # The codecs are the ones in /moov/trak/mdia/minf/stbl/stdsd
    codec_re = re.compile(
        r".*: type (?P<codec>\S+) " r"\(moov.trak.mdia.minf.stbl.stsd.(?P=codec)\)"
    )
    attribute_re = re.compile(r".*: (?P<attribute>\S+) = (?P<value>\d+) .*")
    info_re = re.compile(r".*: info = <\d+ bytes>\s+(?P<byte>\d{2}) .*")
    codecs = {
        mo.group("codec") for mo in [codec_re.match(line) for line in result] if mo
    }
    results = []
    for codec in codecs:
        if codec == "avc1":
            # Look for moov.trak.mdia.minf.stbl.stsd.avc1.avcC
            for idx, line in enumerate(result):
                if "(moov.trak.mdia.minf.stbl.stsd.avc1.avcC)" in line:
                    spaces = len(line) - len(line.lstrip(" "))
                    spaces = " " * (spaces + 1)
                    profile = None
                    constraints = None
                    level = None
                    for line in result[idx + 1 :]:
                        if not line.startswith(spaces):
                            break
                        mo = attribute_re.match(line)
                        if not mo:
                            continue
                        if mo.group("attribute") == "AVCProfileIndication":
                            profile = int(mo.group("value"))
                        elif mo.group("attribute") == "profile_compatibility":
                            constraints = int(mo.group("value"))
                        elif mo.group("attribute") == "AVCLevelIndication":
                            level = int(mo.group("value"))
                    if all(x is not None for x in (profile, constraints, level)):
                        codec = f"avc1.{profile:02x}" f"{constraints:02x}{level:02x}"
                        logger.debug(f"found codec {codec} in {sample}")
                        results.append(codec)
                    else:
                        raise RuntimeError("unable to decode AVC1 codec")
        elif codec == "mp4a":
            # Look for moov.trak.mdia.minf.stbl.stsd.mp4a.esds
            for idx, line in enumerate(result):
                if "(moov.trak.mdia.minf.stbl.stsd.mp4a.esds)" in line:
                    spaces = len(line) - len(line.lstrip(" "))
                    spaces = " " * (spaces + 1)
                    oti = None
                    osti = None
                    for line in result[idx + 1 :]:
                        if not line.startswith(spaces):
                            break
                        mo = attribute_re.match(line)
                        if mo and mo.group("attribute") == "objectTypeId":
                            oti = int(mo.group("value"))
                        elif "decSpecificInfo" in line:
                            osti = ...  # Parse the next line
                        elif osti is ...:
                            mo = info_re.match(line)
                            if not mo:
                                raise RuntimeError("cannot decode " "specific info")
                            osti = (int(mo.group("byte"), 16) & 0xF8) >> 3
                    if all(x is not None for x in (oti, osti)):
                        codec = f"mp4a.{oti:02x}.{osti}"
                        logger.debug(f"found codec {codec} in {sample}")
                        results.append(codec)
                    else:
                        raise RuntimeError("unable to decode MP4A codec")
    return ",".join(results)


def contained_in(original, target):
    """Return dimension to ensure original video fits inside target."""
    ratio = original[0] / original[1]
    width, height = target
    if width / ratio > height:
        width = int(height * ratio)
    else:
        height = int(width / ratio)
    return (width // 2 * 2, height // 2 * 2)


def fix_options(options, technical):
    """Fix options to remove too great sizes."""
    # If no HLS playlist prefix, use an empty one
    if len(options.hls_playlist_prefix) == 0:
        options.hls_playlist_prefix = [""]

    # If audio only is requested, append a width of 0
    if options.audio_only or options.audio_separate:
        options.video_widths.append(0)

    # If needed, extend/truncate video options to the same length
    video_options = [
        option
        for option in vars(options)
        if option.startswith("video_") and option.endswith("s")
    ]
    length = len(options.video_widths)
    for option in video_options:
        value = vars(options)[option]
        del value[length:]
        if option == "video_names":
            continue
        if option == "video_presets" and not value:
            continue
        diff = length - len(value)
        if diff > 0:
            # Copy last value
            value.extend([value[-1]] * diff)

    # Fix bitrate when width is 0
    for idx in range(len(options.video_widths)):
        if options.video_widths[idx] == 0:
            options.video_bitrates[idx] = 0

    # Handle ratio
    options.ratio = operator.truediv(*(int(x) for x in options.ratio.split(":", 1)))

    # Handle video names
    if len(options.video_names) < length:
        diff = length - len(options.video_names)
        more = []
        for idx in range(len(options.video_widths)):
            if options.video_bitrates[idx] > 0:
                more.append(f"{int(options.video_widths[idx]/options.ratio)}p")
            else:
                more.append("Audio only")
        options.video_names.extend(more[-diff:])

    # Add bitrate factor to bitrates
    options.video_bitrates = [
        int(r * options.video_bitrate_factor) for r in options.video_bitrates
    ]
    if options.mp4_bitrate:
        options.mp4_bitrate = int(options.mp4_bitrate * options.video_bitrate_factor)

    # Adapt options depending on video size
    width = technical["video"]["width"]
    height = technical["video"]["height"]

    logger.warning(f"Video is {width} x {height}")

    for idx in reversed(range(len(options.video_widths))):
        if (options.video_widths[idx] > width * 1.1) and (
            options.video_widths[idx] * height / width > height * 1.1
        ):
            logger.warning(f"skip {options.video_widths[idx]} width")
            for option in video_options:
                try:
                    del getattr(options, option)[idx]
                except IndexError:
                    # May happen for video_presets
                    pass
    if not options.poster_width:
        try:
            options.poster_width = max(
                *(w for w in options.video_widths if w <= options.poster_max_width)
            )
        except TypeError:
            options.poster_width = width

    if not options.mp4_width:
        try:
            options.mp4_width = max(
                *(w for w in options.video_widths if w <= options.mp4_max_width)
            )
        except TypeError:
            options.mp4_width = width
    if not options.mp4_bitrate:
        try:
            options.mp4_bitrate = int(
                options.video_bitrates[options.video_widths.index(options.mp4_width)]
                * options.mp4_bitrate_factor
            )
        except Exception:
            options.mp4_bitrate = options.video_bitrates[0] * options.mp4_bitrate_factor


def poster(options, technical):
    """Create poster."""
    if not options.poster:
        logger.debug("skip poster creation")
        return
    logger.debug("create poster")

    # Determine position
    mo = re.match(r"(?:(?P<percent>\d+)%|(?P<seconds>\d+)s)", options.poster_seek)
    if not mo:
        raise RuntimeError(f"invalid value for poster seek: " f"{options.poster_seek}")
    if mo.group("percent"):
        percent = int(mo.group("percent"))
        seek = float(technical["video"]["duration"]) * percent / 100
    else:
        seek = mo.group("seconds")
    seek = int(seek)
    logger.debug(f"seek position for poster is {seek}s")

    # Filter to apply
    vfilter = ["select=eq(pict_type\\,I)"]
    resolution = int(options.poster_width / options.ratio)
    twidth, theight = contained_in(
        (int(technical["video"]["width"]), int(technical["video"]["height"])),
        (options.poster_width, resolution),
    )
    vfilter.append(f"scale={twidth}:{theight}")
    logger.info(f"poster is {twidth}x{theight}")
    if options.poster_grayscale:
        vfilter.append("format=gray")

    # Quality
    quality = options.poster_quality * 30 // 100
    quality = 30 - quality + 1

    args = (
        f"# seek to the given position ({options.poster_seek})",
        "-ss",
        f"{seek}",
        "# load input file",
        "-i",
        f"{options.input}",
        "# only keep first video stream",
        "-map",
        f'0:{technical["video"]["index"]}',
        "# take only one frame",
        "-frames:v",
        "1",
        "# filter to select an I-frame and scale",
        "-vf",
        ",".join(vfilter),
        f"# request a JPEG quality ~ {options.poster_quality}",
        "-qscale:v",
        f"{quality}",
        "# output file",
        options.poster_filename,
    )
    run(
        options,
        "ffmpeg",
        "# only log errors",
        "-loglevel",
        "error",
        "-hide_banner",
        *args,
    )


def transcode(options, technical):
    """Create transcoded files."""
    logger.debug("create transcoded files")
    video = technical["video"]
    audio = technical["audio"]

    # Grab interesting facts about video
    height = int(video["height"])
    width = int(video["width"])
    fps = video["r_frame_rate"]
    fps = operator.truediv(*(int(x) for x in fps.split("/")))
    keyf = math.ceil(fps * options.hls_time)
    logger.info(f"input video is {width}x{height} at {fps:,.2f}fps")
    if audio:
        channels = audio["channels"]
        sampling = audio["sample_rate"]
        logger.info(f"input audio is {channels} channels at {sampling}Hz")

    # Input video
    args = ("# input video", "-i", options.input)
    aargs = ()
    if audio:
        aargs += (
            "# keep the first audio track",
            "-map",
            f'0:{audio["index"]}',
            "# select audio codec",
            "-c:a",
            options.audio_codec,
        )
        if options.audio_sampling:
            aargs += (
                "# set specified sampling rate",
                "-ar",
                f"{options.audio_sampling}",
            )
        else:
            aargs += ("# copy original sampling rate", "-ar", f"{sampling}")
        aargs += (
            "# select audio profile",
            "-profile:a",
            options.audio_profile,
            "# set audio bitrate",
            "-b:a",
            f"{options.audio_bitrate}k",
        )

    # Progressive MP4
    if options.mp4:
        resolution = int(options.mp4_width / options.ratio)
        twidth, theight = contained_in((width, height), (options.mp4_width, resolution))
        logger.info(
            f"progressive MP4 is {twidth}x{theight} at " f"{options.mp4_bitrate}kbps"
        )
        vfilters = [f"scale={twidth}:{theight}", "format=yuv420p"]
        cfilter = "apply filters: scale"
        if options.mp4_overlay:
            with open("_mp4.txt", "w", encoding="utf-8") as f:
                f.write(
                    options.mp4_overlay.format(
                        width=options.mp4_width,
                        resolution=resolution,
                        bitrate=options.mp4_bitrate,
                        codec=options.mp4_codec,
                        profile=options.mp4_profile,
                    )
                )
            vfilters.insert(
                0,
                "drawtext=x=10: y=10: "
                "textfile=_mp4.txt: fontsize=48: "
                "fontcolor=white@0.5: "
                "borderw=3: bordercolor=black@0.5",
            )
            cfilter = "apply filters: add overlay and scale"
        args += (
            "# start producing a progressive MP4",
            "-f",
            "mp4",
            "# keep the first video track",
            "-map",
            f'0:{video["index"]}',
            f"# {cfilter}",
            "-vf",
            ",".join(vfilters),
            "# select video codec",
            "-c:v",
            options.mp4_codec,
            "# select video profile and level",
            "-profile:v",
            options.mp4_profile.split("@")[0],
            "-level:v",
            options.mp4_profile.split("@")[1],
            "# set maximum video bitrate",
            "-b:v",
            f"{options.mp4_bitrate}k",
            "-maxrate:v",
            f"{options.mp4_bitrate}k",
            "-bufsize:v",
            f"{options.mp4_bitrate*1.5}k",
        )
        if options.mp4_preset:
            args += ("# set the video preset", "-preset", options.mp4_preset)
        args += aargs
        args += (
            "# move index at the beginning",
            "-movflags",
            "+faststart",
            "# output filename",
            options.mp4_filename,
        )

    # HLS
    playlists = {}
    for idx in range(len(options.video_widths)):
        logger.debug(f"setup HLS for {options.video_names[idx]}")
        resolution = int(options.video_widths[idx] / options.ratio)
        voptions = dict(
            width=options.video_widths[idx],
            resolution=resolution,
            bitrate=options.video_bitrates[idx],
            codec=options.video_codecs[idx],
            name=options.video_names[idx],
            profile=options.video_profiles[idx],
        )
        twidth, theight = contained_in(
            (width, height), (options.video_widths[idx], resolution)
        )
        vfilters = [f"scale={twidth}:{theight}", "format=yuv420p"]
        cfilter = "apply filters: scale"
        if options.video_overlay:
            with open(f"_{idx}.txt", "w") as f:
                f.write(options.video_overlay.format(**voptions))
            vfilters.insert(
                0,
                "drawtext=x=10: y=10: "
                f"textfile=_{idx}.txt: fontsize=48: "
                "fontcolor=white@0.5: "
                "borderw=3: bordercolor=black@0.5",
            )
            cfilter = "apply filters: add overlay and scale"
        vargs = ()
        if options.video_bitrates[idx] > 0:
            vargs += (
                "# keep the first video track",
                "-map",
                f'0:{video["index"]}',
                f"# {cfilter}",
                "-vf",
                ",".join(vfilters),
                "# select video codec",
                "-c:v",
                options.video_codecs[idx],
                "# select video profile and level",
                "-profile:v",
                options.video_profiles[idx].split("@")[0],
                "-level:v",
                options.video_profiles[idx].split("@")[1],
                "# set maximum video bitrate",
                "-b:v",
                f"{options.video_bitrates[idx]}k",
                "-maxrate:v",
                f"{options.video_bitrates[idx]}k",
                "-bufsize:v",
                f"{options.video_bitrates[idx]*1.5}k",
            )
            if options.video_presets:
                vargs += (
                    "# set the video preset",
                    "-preset",
                    f"{options.video_presets[idx]}",
                )
        else:
            vargs += ("# no video",)
        # HLS options
        args += (
            f"# start producing HLS segments for {resolution}p ({idx})",
            "-f",
            "hls",
            *vargs,
            *(
                aargs
                if not options.audio_separate or options.video_bitrates[idx] == 0
                else ()
            ),
            "# duration of an HLS segment",
            "-hls_time",
            f"{options.hls_time}",
            "# this is fairly important:",
            f"# set I-frame at the beginning of each segment (fps={fps:,.3f})",
            "-g",
            f"{keyf}",
            "-keyint_min",
            f"{keyf}",
            "# set HLS playlist type",
            "-hls_playlist_type",
            "vod",
            "# do not limit playlist size",
            "-hls_list_size",
            "0",
            "# use fMP4 (iOS > 10)"
            if options.hls_type == "fmp4"
            else "# use MPEG2-TS (compatible with any iOS)",
            "-hls_segment_type",
            options.hls_type,
            "# append a base URL to each segment name",
            "-hls_base_url",
            options.hls_segment_prefix,
            "# set pattern for segment filenames",
            "-hls_segment_filename",
            ".".join(
                [
                    options.hls_segments.format(index=f"{idx}_%03d", **voptions),
                    {"mpegts": "ts", "fmp4": "mp4"}[options.hls_type],
                ]
            ),
        )
        if options.hls_type == "fmp4":
            args += (
                "# filename for initial fMP4 segment",
                "-hls_fmp4_init_filename",
                ".".join(
                    [
                        options.hls_segments.format(index=f"{idx}_init", **voptions),
                        "mp4",
                    ]
                ),
            )
        # Output filename
        playlist_name = ".".join(
            [options.hls_segments.format(index=f"{idx}", **voptions), "m3u8"]
        )
        args += (playlist_name,)
        playlists[idx] = {"name": playlist_name, "resolution": f"{twidth}x{theight}"}

        # Small MP4 to extract codec
        if options.hls_add_codecs:
            args += (
                "# also generate a small MP4 to extract codec later",
                "-f",
                "mp4",
                "# use same encoding arguments as the normal video:",
                *vargs,
                *aargs,
                "# but keep only one frame",
                "-frames:v",
                "1",
                "# put the result into a temporary file",
                f"_{idx}.mp4",
            )

    logger.info("start transcoding")
    run(
        options,
        "ffmpeg",
        "# only log errors",
        "-loglevel",
        "error",
        "-hide_banner",
        *args,
    )

    logger.info("write master playlist")
    no_codecs = False
    with open(options.hls_master_playlist, "w", encoding="utf-8") as master:
        master.write("#EXTM3U\n")
        if options.audio_separate:
            master.write("#EXT-X-VERSION:4\n")
            for prefix in options.hls_playlist_prefix:
                master.write(
                    '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",'
                    "DEFAULT=yes,AUTOSELECT=yes,"
                    f'URI="{prefix}'
                    f'{playlists[max(playlists.keys())]["name"]}"\n'
                )
        else:
            master.write("#EXT-X-VERSION:3\n")
        for idx in playlists:
            if not options.audio_only and options.video_bitrates[idx] == 0:
                # We didn't ask for an audio only track but we have
                # one, skip it.
                continue
            codecs = None
            if not no_codecs and options.hls_add_codecs:
                try:
                    codecs = extract_codecs(options, f"_{idx}.mp4")
                except (FileNotFoundError, RuntimeError):
                    logger.warning("cannot extract codec due to " "mp4file missing")
                    no_codecs = True
            bandwidth = options.video_bitrates[idx]
            if audio:
                bandwidth += options.audio_bitrate
            for prefix in options.hls_playlist_prefix:
                master.write("#EXT-X-STREAM-INF:" f"BANDWIDTH={bandwidth}000,")
                if options.video_bitrates[idx] > 0:
                    master.write(
                        f'RESOLUTION={playlists[idx]["resolution"]},'
                        f"FRAME-RATE={fps:,.3f},"
                    )
                if codecs:
                    master.write(f'CODECS="{codecs}",')
                if options.audio_separate:
                    master.write('AUDIO="audio",')
                master.write(
                    f'NAME="{options.video_names[idx]}"\n'
                    f'{prefix}{playlists[idx]["name"]}\n'
                )

    codecs = None
    if not no_codecs and options.hls_add_codecs and options.mp4:
        try:
            codecs = extract_codecs(options, options.mp4_filename)
        except (FileNotFoundError, RuntimeError):
            pass
    codecs = f'; codecs="{codecs}"' if codecs else ""
    poster = f' poster="{options.poster_filename}"' if options.poster else ""

    vt = f""""
<video{poster} controls>
    <source src="index.m3u8" type="application/vnd.apple.mpegurl">"""

    if options.mp4:
        vt += f'      <source src="{options.mp4_filename}"'
        vt += f"type='video/mp4{codecs}'>"

    vt += "    </video>"

    with open("video-tag.html", "w", encoding="utf-8") as f:
        f.write(vt)
