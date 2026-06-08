import av
import numpy as np
from fractions import Fraction
from collections import deque
import time


# Fine container time base for video so VFR PTS derived from capture wall-clock
# can represent real frame timing (and real gaps from dropped/late frames).
VIDEO_TIME_BASE = Fraction(1, 90000)


def _measure_true_rate(chunks, nominal_rate):
    """Estimate a capture device's *real* sample rate from the wall-clock stamps
    attached to each recorded chunk.

    Audio devices run on their own crystal which is never exactly the nominal
    rate (e.g. 44098.7 Hz instead of 44100). Over a long recording that error
    accumulates into gross A/V desync. We recover the true rate from
    total_samples / real_elapsed and resample back onto the nominal grid.

    Returns nominal_rate when there isn't enough signal to measure or the
    estimate is implausible (e.g. a long mid-record stall)."""
    if not chunks or len(chunks) < 3:
        return float(nominal_rate)
    total_samples = sum(len(c[0]) for c in chunks)
    first_ts = chunks[0][1]
    last_ts = chunks[-1][1]
    # Each stamp is taken AFTER a blocking record() of one chunk, so the first
    # chunk's samples span [first_ts - first_chunk_dur, first_ts].
    first_chunk_dur = len(chunks[0][0]) / float(nominal_rate)
    elapsed = (last_ts - first_ts) + first_chunk_dur
    if elapsed <= 0 or total_samples <= 0:
        return float(nominal_rate)
    measured = total_samples / elapsed
    # Guard against absurd estimates; real device drift is well under 5%.
    if not (nominal_rate * 0.95 <= measured <= nominal_rate * 1.05):
        return float(nominal_rate)
    return measured


def _resample_audio(data, src_rate, dst_rate):
    """Resample (N, 2) float audio from a (possibly fractional) src_rate to
    dst_rate using libswresample via av.AudioResampler.

    Scaled-integer rates give sub-Hz ratio precision, which is what the
    ppm-level clock-drift correction needs (a rational up/down resampler would
    need astronomically large factors). Fed in slices to bound peak memory; swr
    keeps state across calls so this is equivalent to a single pass."""
    if data is None or len(data) == 0:
        return data
    if abs(src_rate - dst_rate) < 1e-4:
        return data
    SCALE = 100  # 0.01 Hz ratio resolution
    in_rate = int(round(src_rate * SCALE))
    out_rate = int(round(dst_rate * SCALE))
    if in_rate <= 0 or out_rate <= 0:
        return data

    resampler = av.AudioResampler(format='fltp', layout='stereo', rate=out_rate)
    out_parts = []
    SLICE = 1 << 18  # 262144 samples per feed
    n = len(data)
    for start in range(0, n, SLICE):
        block = np.ascontiguousarray(data[start:start + SLICE].T, dtype=np.float32)
        frame = av.AudioFrame.from_ndarray(block, format='fltp', layout='stereo')
        frame.sample_rate = in_rate
        for out_frame in resampler.resample(frame):
            out_parts.append(out_frame.to_ndarray())
    for out_frame in resampler.resample(None):  # flush
        out_parts.append(out_frame.to_ndarray())

    if not out_parts:
        return data[:0]
    out = np.concatenate(out_parts, axis=1).T  # (total, 2)
    return np.ascontiguousarray(out, dtype=np.float32)


def _align_track(data, device_first_sample_time, t0, target_len, dst_rate):
    """Shift `data` so its first real sample sits at the shared origin t0
    (drop the head if the device started before t0, prepend silence if it
    started after), then trim/pad the tail to target_len samples.

    This is the only place a front offset is applied, computed from real
    wall-clock times rather than frame-index/nominal-rate math, so it fixes
    both per-stream startup latency and mic-vs-system phase. It happens before
    any audio plays, so it is inaudible."""
    if data is None:
        data = np.zeros((0, 2), dtype=np.float32)
    dtype = data.dtype if data.size else np.float32

    offset = int(round((t0 - device_first_sample_time) * dst_rate))
    if offset > 0:
        data = data[offset:] if offset < len(data) else data[:0]
    elif offset < 0:
        pad = np.zeros((-offset, 2), dtype=dtype)
        data = np.vstack([pad, data]) if len(data) else pad

    if target_len is not None and target_len >= 0:
        if len(data) > target_len:
            data = data[:target_len]
        elif len(data) < target_len:
            pad = np.zeros((target_len - len(data), 2), dtype=dtype)
            data = np.vstack([data, pad]) if len(data) else pad
    return data


def _normalize(data):
    if data is None or not data.size:
        return data
    max_val = np.max(np.abs(data))
    if max_val > 1.0:
        data = data / max_val
    data = np.nan_to_num(data, nan=0.0, posinf=1.0, neginf=-1.0)
    return np.clip(data, -1.0, 1.0)


def encode_streams(output_path, video_frames=None, mic_audio=None, sys_audio=None,
                   video_fps=60, audio_sample_rate=44100, discard_from_beginning=0.0, split_audio_tracks=False):
    start_time = time.time()
    MAX_VIDEO_FRAME_BATCH_SIZE = 60
    AUDIO_CHUNK_SIZE = 32768

    # Determine output format and file extension
    has_video = video_frames is not None and len(video_frames) > 0
    has_audio = (mic_audio is not None and len(mic_audio) > 0) or (sys_audio is not None and len(sys_audio) > 0)

    if has_video:
        output_file = f"{output_path}.mp4"
        container_format = 'mp4'
    elif has_audio:
        output_file = f"{output_path}.mp3"
        container_format = 'mp3'
    else:
        print("No streams provided!")
        return

    # Open output container
    container = av.open(output_file, mode='w', format=container_format)

    try:
        audio_stream = None
        mic_audio_stream = None
        sys_audio_stream = None
        video_stream = None

        if has_video:
            source_stream_from_packet = video_frames[0][2]
            video_stream = container.add_stream(source_stream_from_packet.name)
            video_stream.width = source_stream_from_packet.width
            video_stream.height = source_stream_from_packet.height
            video_stream.pix_fmt = source_stream_from_packet.pix_fmt
            # VFR: PTS are derived per-frame from capture wall-clock, so use a
            # fine fixed time base rather than the nominal 1/fps.
            video_stream.time_base = VIDEO_TIME_BASE

        # Set up audio stream(s) if we have audio data
        if has_audio:
            audio_codec_name = 'aac'
            if container_format == 'mp3':
                audio_codec_name = 'mp3'

            if split_audio_tracks:
                if mic_audio is not None and len(mic_audio) > 0:
                    mic_audio_stream = container.add_stream(audio_codec_name, rate=audio_sample_rate)
                    mic_audio_stream.layout = 'stereo'
                    mic_audio_stream.sample_rate = audio_sample_rate
                    mic_audio_stream.time_base = Fraction(1, audio_sample_rate)

                if sys_audio is not None and len(sys_audio) > 0:
                    sys_audio_stream = container.add_stream(audio_codec_name, rate=audio_sample_rate)
                    sys_audio_stream.layout = 'stereo'
                    sys_audio_stream.sample_rate = audio_sample_rate
                    sys_audio_stream.time_base = Fraction(1, audio_sample_rate)
            else:
                audio_stream = container.add_stream(audio_codec_name, rate=audio_sample_rate)
                audio_stream.layout = 'stereo'
                audio_stream.sample_rate = audio_sample_rate
                audio_stream.time_base = Fraction(1, audio_sample_rate)

        # ----------------------------------------------------------------
        # Master clock: pick a single origin t0 and the real video duration
        # from capture wall-clock stamps. Everything below is aligned to t0.
        # ----------------------------------------------------------------
        target_start_frame_idx = 0
        if has_video:
            if discard_from_beginning > 0.0:
                target_start_frame_idx = int(discard_from_beginning * video_fps)
            target_start_frame_idx = max(0, min(target_start_frame_idx, len(video_frames) - 1))
            t0 = video_frames[target_start_frame_idx][1]
            video_real_duration = max(0.0, video_frames[-1][1] - t0)
        else:
            # Audio-only: anchor to the earliest real sample across devices.
            firsts = []
            for chunks in (mic_audio, sys_audio):
                if chunks:
                    firsts.append(chunks[0][1] - len(chunks[0][0]) / float(audio_sample_rate))
            t0 = min(firsts) if firsts else 0.0
            lasts = [chunks[-1][1] for chunks in (mic_audio, sys_audio) if chunks]
            video_real_duration = (max(lasts) - t0) if lasts else 0.0

        # When there's video, conform audio length to the real video duration.
        target_audio_len = int(round(video_real_duration * audio_sample_rate)) if (has_video and has_audio) else None

        # ----------------------------------------------------------------
        # Audio: concat -> measure true device rate -> resample onto the
        # nominal grid -> align each device to t0 -> conform tail.
        # ----------------------------------------------------------------
        def _prep_device(chunks):
            if not chunks:
                return None
            data = np.concatenate([c[0] for c in chunks], axis=0)
            if data.ndim == 1:
                data = np.expand_dims(data, axis=1)
            if data.shape[1] == 1:
                data = np.hstack((data, data))
            data = np.ascontiguousarray(data, dtype=np.float32)

            measured = _measure_true_rate(chunks, audio_sample_rate)
            data = _resample_audio(data, measured, audio_sample_rate)

            first_sample_time = chunks[0][1] - len(chunks[0][0]) / float(audio_sample_rate)
            data = _align_track(data, first_sample_time, t0, target_audio_len, audio_sample_rate)
            return data

        mic_data = _prep_device(mic_audio) if (has_audio and mic_audio) else None
        sys_data = _prep_device(sys_audio) if (has_audio and sys_audio) else None

        combined_audio = None
        if has_audio and not split_audio_tracks:
            if mic_data is not None and sys_data is not None:
                n = max(len(mic_data), len(sys_data))
                if len(mic_data) < n:
                    mic_data = np.vstack([mic_data, np.zeros((n - len(mic_data), 2), dtype=np.float32)])
                if len(sys_data) < n:
                    sys_data = np.vstack([sys_data, np.zeros((n - len(sys_data), 2), dtype=np.float32)])
                combined_audio = mic_data + sys_data
            elif mic_data is not None:
                combined_audio = mic_data
            elif sys_data is not None:
                combined_audio = sys_data
            combined_audio = _normalize(combined_audio)
        elif has_audio and split_audio_tracks:
            mic_data = _normalize(mic_data)
            sys_data = _normalize(sys_data)

        # ----------------------------------------------------------------
        # Video muxing: PTS/DTS derived from each frame's capture timestamp
        # (relative to t0), so dropped/late frames keep real timing instead
        # of the old nominal-CFR counter. Original ring-buffer packets are
        # cloned before stamping so the live buffer is never mutated.
        # ----------------------------------------------------------------
        _last_pts = [-1]

        def mux_video_packet(packet, stamp, clone):
            if clone:
                kf = packet.is_keyframe
                packet = av.Packet(bytes(packet))
                packet.is_keyframe = kf
            pts = int(round((stamp - t0) * 90000))  # seconds -> 1/90000 units
            if pts <= _last_pts[0]:
                pts = _last_pts[0] + 1  # keep DTS strictly monotonic
            _last_pts[0] = pts
            packet.pts = pts
            packet.dts = pts
            packet.time_base = VIDEO_TIME_BASE
            packet.stream = video_stream
            container.mux(packet)

        start_muxing_original_video_idx = 0

        if has_video and video_stream:
            target_idx = target_start_frame_idx

            # Find the first natural I-frame at or after the desired start.
            reencode_end_original_idx = -1
            for idx in range(target_idx, len(video_frames)):
                for pkt in video_frames[idx][0]:
                    if pkt.is_keyframe:
                        reencode_end_original_idx = idx
                        break
                if reencode_end_original_idx != -1:
                    break
            if reencode_end_original_idx == -1:
                reencode_end_original_idx = len(video_frames)
                if discard_from_beginning > 0.0:
                    print("Warning: No natural keyframe found after discard point. Re-encoding to end of provided video frames.")

            if discard_from_beginning > 0.0:
                print(f"Re-encoding initial segment (original indices 0 to {reencode_end_original_idx - 1}) to force keyframe at original frame {target_idx}.")

                # Temp decoder (generic h264) + temp encoder (source codec) to
                # manufacture a clean keyframe at the exact requested start.
                temp_decoder = av.CodecContext.create('h264', 'r')
                temp_decoder.width = source_stream_from_packet.width
                temp_decoder.height = source_stream_from_packet.height
                temp_decoder.pix_fmt = source_stream_from_packet.pix_fmt
                temp_decoder.options = source_stream_from_packet.options
                temp_decoder.open()

                temp_encoder = av.CodecContext.create(source_stream_from_packet.name, 'w')
                temp_encoder.width = source_stream_from_packet.width
                temp_encoder.height = source_stream_from_packet.height
                temp_encoder.pix_fmt = source_stream_from_packet.pix_fmt
                temp_encoder.time_base = source_stream_from_packet.time_base
                temp_encoder.options = source_stream_from_packet.options
                temp_encoder.open()

                decoded_frames = []
                for idx in range(reencode_end_original_idx):
                    for pkt in video_frames[idx][0]:
                        for frame in temp_decoder.decode(pkt):
                            decoded_frames.append(frame)
                for frame in temp_decoder.decode(None):  # flush
                    decoded_frames.append(frame)

                # Re-encode from the desired start; map each emitted packet back
                # to its source frame's wall-clock stamp (decode order == display
                # order since the recorder emits no B-frames).
                pending_head_stamps = deque()
                last_head_stamp = t0
                for i, frame in enumerate(decoded_frames):
                    if i >= target_idx:
                        if i == target_idx:
                            frame.pict_type = av.video.frame.PictureType.I
                        frame.pts = i - target_idx  # relative PTS for the temp encoder
                        stamp_i = video_frames[i][1] if i < len(video_frames) else last_head_stamp
                        pending_head_stamps.append(stamp_i)
                        last_head_stamp = stamp_i
                        for new_packet in temp_encoder.encode(frame):
                            s = pending_head_stamps.popleft() if pending_head_stamps else last_head_stamp
                            mux_video_packet(new_packet, s, clone=False)
                for new_packet in temp_encoder.encode(None):  # flush
                    s = pending_head_stamps.popleft() if pending_head_stamps else last_head_stamp
                    mux_video_packet(new_packet, s, clone=False)

                start_muxing_original_video_idx = reencode_end_original_idx
            else:
                start_muxing_original_video_idx = 0
                print("No video re-encoding or trimming needed (discard_from_beginning is 0).")

        # Stream-copy the remaining original packets, timestamped from capture.
        if has_video and video_stream:
            for batch_start in range(start_muxing_original_video_idx, len(video_frames), MAX_VIDEO_FRAME_BATCH_SIZE):
                batch_end = min(batch_start + MAX_VIDEO_FRAME_BATCH_SIZE, len(video_frames))
                for frame_idx in range(batch_start, batch_end):
                    stamp = video_frames[frame_idx][1]
                    for packet in video_frames[frame_idx][0]:
                        mux_video_packet(packet, stamp, clone=True)

        # ----------------------------------------------------------------
        # Audio muxing. Data is already aligned to t0 and on the nominal grid,
        # so sample-count PTS are now correct.
        # ----------------------------------------------------------------
        if has_audio:
            chunk_size = AUDIO_CHUNK_SIZE

            def encode_audio(stream, data):
                if stream is None or data is None or not len(data):
                    return
                total = len(data)
                for sample_idx in range(0, total, chunk_size):
                    end_sample = min(sample_idx + chunk_size, total)
                    audio_chunk = data[sample_idx:end_sample]
                    if len(audio_chunk) > 0:
                        audio_chunk_f32 = np.ascontiguousarray(audio_chunk.T, dtype=np.float32)
                        audio_frame = av.AudioFrame.from_ndarray(
                            audio_chunk_f32, format='fltp', layout='stereo'
                        )
                        audio_frame.sample_rate = audio_sample_rate
                        audio_frame.pts = sample_idx
                        audio_frame.time_base = stream.time_base
                        for packet in stream.encode(audio_frame):
                            container.mux(packet)

            if split_audio_tracks:
                encode_audio(mic_audio_stream, mic_data)
                encode_audio(sys_audio_stream, sys_data)
            else:
                encode_audio(audio_stream, combined_audio)

        # Flush audio encoders
        if has_audio:
            if split_audio_tracks:
                if mic_audio_stream:
                    for packet in mic_audio_stream.encode():
                        container.mux(packet)
                if sys_audio_stream:
                    for packet in sys_audio_stream.encode():
                        container.mux(packet)
            else:
                if audio_stream:
                    for packet in audio_stream.encode():
                        container.mux(packet)

    finally:
        container.close()

    end_time = time.time()

    print(f"({end_time - start_time}s)Successfully encoded to: {output_file}")
