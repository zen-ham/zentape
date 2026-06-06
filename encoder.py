import av
import numpy as np
from fractions import Fraction
import time


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
            # source_stream_from_packet = first_packet_from_external.stream
            video_stream = container.add_stream(source_stream_from_packet.name)
            video_stream.width = source_stream_from_packet.width
            video_stream.height = source_stream_from_packet.height
            video_stream.pix_fmt = source_stream_from_packet.pix_fmt
            video_stream.time_base = source_stream_from_packet.time_base

        # Set up audio stream(s) if we have audio data
        if has_audio:
            audio_codec_name = 'aac'
            if container_format == 'mp3':
                audio_codec_name = 'mp3'

            if split_audio_tracks:
                # Create separate streams for mic and system audio
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
                # Original behavior - single combined audio stream
                audio_stream = container.add_stream(audio_codec_name, rate=audio_sample_rate)
                audio_stream.layout = 'stereo'
                audio_stream.sample_rate = audio_sample_rate
                audio_stream.time_base = Fraction(1, audio_sample_rate)

        # OPTIMIZATION: Pre-process ALL audio data once instead of per-frame
        combined_audio = None
        mic_data = None
        sys_data = None

        if has_audio:
            # print("Pre-processing audio data...")

            if mic_audio:
                mic_chunks = [chunk[0] for chunk in mic_audio]
                mic_data = np.concatenate(mic_chunks, axis=0)
                if mic_data.ndim == 1:
                    mic_data = np.expand_dims(mic_data, axis=1)
                if mic_data.shape[1] == 1:
                    mic_data = np.hstack((mic_data, mic_data))

            if sys_audio:
                sys_chunks = [chunk[0] for chunk in sys_audio]
                sys_data = np.concatenate(sys_chunks, axis=0)
                if sys_data.ndim == 1:
                    sys_data = np.expand_dims(sys_data, axis=1)
                if sys_data.shape[1] == 1:
                    sys_data = np.hstack((sys_data, sys_data))

            if not split_audio_tracks:
                # Combine audio streams for original behavior
                if mic_data is not None and sys_data is not None:
                    min_len = min(len(mic_data), len(sys_data))
                    combined_audio = mic_data[:min_len] + sys_data[:min_len]
                elif mic_data is not None:
                    combined_audio = mic_data
                elif sys_data is not None:
                    combined_audio = sys_data

                # Normalize once
                if combined_audio is not None:
                    max_val = np.max(np.abs(combined_audio))
                    if max_val > 1.0:
                        combined_audio = combined_audio / max_val
                    # Ensure no NaNs/Infs and clip to valid range for encoder
                    combined_audio = np.nan_to_num(combined_audio, nan=0.0, posinf=1.0, neginf=-1.0)
                    combined_audio = np.clip(combined_audio, -1.0, 1.0)
            else:
                # Normalize separate audio streams
                if mic_data is not None:
                    max_val = np.max(np.abs(mic_data))
                    if max_val > 1.0:
                        mic_data = mic_data / max_val
                    # Ensure no NaNs/Infs and clip to valid range for encoder
                    mic_data = np.nan_to_num(mic_data, nan=0.0, posinf=1.0, neginf=-1.0)
                    mic_data = np.clip(mic_data, -1.0, 1.0)

                if sys_data is not None:
                    max_val = np.max(np.abs(sys_data))
                    if max_val > 1.0:
                        sys_data = sys_data / max_val
                    # Ensure no NaNs/Infs and clip to valid range for encoder
                    sys_data = np.nan_to_num(sys_data, nan=0.0, posinf=1.0, neginf=-1.0)
                    sys_data = np.clip(sys_data, -1.0, 1.0)

        # Calculate synchronization parameters
        # total_frames here refers to the *input* number of video frames before trimming
        total_frames = len(video_frames) if has_video else 0
        # total_samples here refers to the *input* number of audio samples before trimming
        if split_audio_tracks:
            total_samples_mic = len(mic_data) if mic_data is not None else 0
            total_samples_sys = len(sys_data) if sys_data is not None else 0
        else:
            total_samples = len(combined_audio) if has_audio and combined_audio is not None else 0

        if has_video and has_audio:
            video_duration = total_frames / video_fps
            expected_audio_samples = int(video_duration * audio_sample_rate)

            if split_audio_tracks:
                if mic_data is not None:
                    if len(mic_data) > expected_audio_samples:
                        mic_data = mic_data[:expected_audio_samples]
                    elif len(mic_data) < expected_audio_samples:
                        padding = np.zeros((expected_audio_samples - len(mic_data), 2))
                        mic_data = np.vstack([mic_data, padding])
                    total_samples_mic = len(mic_data)

                if sys_data is not None:
                    if len(sys_data) > expected_audio_samples:
                        sys_data = sys_data[:expected_audio_samples]
                    elif len(sys_data) < expected_audio_samples:
                        padding = np.zeros((expected_audio_samples - len(sys_data), 2))
                        sys_data = np.vstack([sys_data, padding])
                    total_samples_sys = len(sys_data)
            else:
                if combined_audio is not None:
                    if len(combined_audio) > expected_audio_samples:
                        combined_audio = combined_audio[:expected_audio_samples]
                    elif len(combined_audio) < expected_audio_samples:
                        padding = np.zeros((expected_audio_samples - len(combined_audio), 2))
                        combined_audio = np.vstack([combined_audio, padding])
                else:
                    combined_audio = np.zeros((expected_audio_samples, 2))
                total_samples = len(combined_audio)

        # --- KEYFRAME FIX AND TRIMMING LOGIC ---
        output_frame_counter = 0  # This will be the pts/dts for the output video stream
        # output_frame_counter_dts = 0
        # dts_buffer = 5

        # def calculate_cdts():
        #    return max(0, output_frame_counter-dts_buffer)

        start_muxing_original_video_idx = 0  # Index in `video_frames` to start muxing pre-encoded packets from
        actual_video_start_frame_idx = 0  # Track the actual frame index where output video starts

        if has_video and video_stream:
            # Calculate the target frame index in the *original* `video_frames` list
            # where the output video should ideally begin. This frame must be an I-frame.
            target_start_frame_idx_in_original_video = int(discard_from_beginning * video_fps)

            # Determine the segment to re-encode.
            # We re-encode from the very beginning of the provided `video_frames` (index 0),
            # up to the first natural I-frame that occurs *after* our `target_start_frame_idx_in_original_video`.
            # This ensures we have enough frames to decode and re-encode, forcing an I-frame
            # at our desired start point, and then seamlessly switch back to original packets.
            reencode_end_original_idx = -1  # Marks the first original frame to mux after re-encode

            # Find the first I-frame in the original `video_frames` that is at or after
            # `target_start_frame_idx_in_original_video`.
            for idx in range(target_start_frame_idx_in_original_video, len(video_frames)):
                packets_list_at_idx = video_frames[idx][0]
                for pkt in packets_list_at_idx:
                    if pkt.is_keyframe:
                        reencode_end_original_idx = idx
                        break
                if reencode_end_original_idx != -1:
                    break

            if reencode_end_original_idx == -1:
                # If no I-frame found after the target_start_frame_idx_in_original_video,
                # we must re-encode up to the very end of the provided video_frames.
                reencode_end_original_idx = len(video_frames)
                if discard_from_beginning > 0.0:
                    print("Warning: No natural keyframe found after discard point. Re-encoding to end of provided video frames.")

            # Only proceed with re-encoding if there's an actual discard or if the first frame isn't truly the start.
            # Given the upstream ensures an I-frame at [0], re-encoding is mainly for discard > 0.
            if discard_from_beginning > 0.0:
                print(f"Re-encoding initial segment (original indices 0 to {reencode_end_original_idx - 1}) to force keyframe at original frame {target_start_frame_idx_in_original_video}.")

                # Setup temporary decoder (using generic h264 for decoding)
                temp_decoder = av.CodecContext.create('h264', 'r')
                temp_decoder.width = source_stream_from_packet.width
                temp_decoder.height = source_stream_from_packet.height
                temp_decoder.pix_fmt = source_stream_from_packet.pix_fmt
                temp_decoder.options = source_stream_from_packet.options
                # temp_decoder.time_base = source_stream_from_packet.time_base  it doesn't actually take this variable
                temp_decoder.open()

                # Setup temporary encoder (using the source codec name for encoding)
                temp_encoder = av.CodecContext.create(source_stream_from_packet.name, 'w')
                temp_encoder.width = source_stream_from_packet.width
                temp_encoder.height = source_stream_from_packet.height
                temp_encoder.pix_fmt = source_stream_from_packet.pix_fmt
                temp_encoder.time_base = source_stream_from_packet.time_base
                temp_encoder.options = source_stream_from_packet.options  # Common quality setting for H.264
                temp_encoder.open()

                decoded_frames = []
                # Decode the segment from original index 0 up to (but not including) `reencode_end_original_idx`
                for idx in range(reencode_end_original_idx):
                    packets_list = video_frames[idx][0]
                    for pkt in packets_list:
                        for frame in temp_decoder.decode(pkt):
                            decoded_frames.append(frame)

                # Flush the decoder to get any remaining buffered frames
                for frame in temp_decoder.decode(None):
                    decoded_frames.append(frame)
                # temp_decoder.close() # Keep this commented out if it's causing issues, or ensure it's properly handled.

                # Re-encode these decoded frames, forcing a keyframe at `target_start_frame_idx_in_original_video`
                # and only muxing packets that are at or after this point.
                for i, frame in enumerate(decoded_frames):
                    # Only consider frames that are at or after the desired start point in the original sequence
                    if i >= target_start_frame_idx_in_original_video:
                        # Force keyframe at the exact target start frame index by setting pict_type
                        if i == target_start_frame_idx_in_original_video:
                            frame.pict_type = av.video.frame.PictureType.I  # <--- MODIFIED LINE
                            actual_video_start_frame_idx = i  # This is where video actually starts

                        # Set PTS for the input frame to the encoder, relative to the *decoded* sequence.
                        # This helps the encoder maintain its internal state for P/B frames.
                        frame.pts = i - target_start_frame_idx_in_original_video  # Relative PTS for temp encoder

                        # Removed **encode_options since 'keyframe' is not a valid argument
                        for new_packet in temp_encoder.encode(frame):  # <--- MODIFIED LINE
                            new_packet.stream = video_stream
                            # Assign global PTS/DTS to the new packets, starting from 0 for the output.
                            # new_packet.pts = output_frame_counter
                            # new_packet.dts = output_frame_counter
                            container.mux(new_packet)
                            output_frame_counter += 1

                # Flush the temporary encoder for any remaining packets
                for new_packet in temp_encoder.encode(None):
                    new_packet.stream = video_stream
                    # new_packet.pts = output_frame_counter
                    # new_packet.dts = output_frame_counter
                    container.mux(new_packet)
                    output_frame_counter += 1

                # Update the index from which to start muxing the original (un-re-encoded) video packets
                start_muxing_original_video_idx = reencode_end_original_idx
            else:
                # If discard_from_beginning is 0.0, no re-encoding is needed for video.
                # The upstream guarantees an I-frame at the start.
                start_muxing_original_video_idx = 0
                actual_video_start_frame_idx = 0
                print("No video re-encoding or trimming needed (discard_from_beginning is 0).")
        # --- END KEYFRAME FIX AND TRIMMING LOGIC ---

        # Adjust audio data based on the ACTUAL video start frame, not the target
        if has_audio:
            if has_video:
                # Calculate discard samples based on where the video actually starts
                actual_discard_time = actual_video_start_frame_idx / video_fps
                discard_samples_count = int(actual_discard_time * audio_sample_rate)
            else:
                # If no video, use the original discard_from_beginning parameter
                discard_samples_count = int(discard_from_beginning * audio_sample_rate)

            if discard_samples_count > 0:
                print(f"Discarding {discard_samples_count} audio samples from the beginning (actual video start at frame {actual_video_start_frame_idx}).")
                if split_audio_tracks:
                    if mic_data is not None:
                        mic_data = mic_data[discard_samples_count:]
                        total_samples_mic = len(mic_data)
                    if sys_data is not None:
                        sys_data = sys_data[discard_samples_count:]
                        total_samples_sys = len(sys_data)
                else:
                    if combined_audio is not None:
                        combined_audio = combined_audio[discard_samples_count:]
                        total_samples = len(combined_audio)
            else:
                print("No audio samples discarded.")

        # Process remaining video frames in batches (original logic, starting from adjusted index)
        if has_video and video_stream:
            # Calculate the total frames that will be in the output video (re-encoded + original)
            remaining_original_frames = len(video_frames) - start_muxing_original_video_idx
            total_frames_in_output = output_frame_counter + remaining_original_frames

            # Adjust batch_size for the remaining frames
            # The range starts from `start_muxing_original_video_idx`
            for batch_start_idx_original in range(start_muxing_original_video_idx, len(video_frames), MAX_VIDEO_FRAME_BATCH_SIZE):
                batch_end_idx_original = min(batch_start_idx_original + MAX_VIDEO_FRAME_BATCH_SIZE, len(video_frames))

                for frame_idx in range(batch_start_idx_original, batch_end_idx_original):
                    packets = video_frames[frame_idx][0]

                    # mux packets
                    for packet in packets:
                        packet.stream = video_stream
                        # Continue global PTS/DTS counter for the output stream
                        packet.dts = output_frame_counter
                        packet.pts = output_frame_counter
                        container.mux(packet)
                        output_frame_counter += 1

                # Show progress (relative to the total frames that will be output)
                if total_frames_in_output > 0:
                    current_progress_frames = output_frame_counter
                    progress = (current_progress_frames / total_frames_in_output) * 100
                    # print(f"Video encoding progress (original packets): {progress:.1f}%")

        # OPTIMIZATION: Process audio in larger, efficient chunks
        if has_audio:
            # print("Encoding audio...")
            # Use much larger audio chunks for efficiency
            chunk_size = AUDIO_CHUNK_SIZE  # Larger chunks are more efficient

            if split_audio_tracks:
                # Process mic audio stream
                if mic_audio_stream and mic_data is not None:
                    for sample_idx in range(0, total_samples_mic, chunk_size):
                        end_sample = min(sample_idx + chunk_size, total_samples_mic)
                        audio_chunk = mic_data[sample_idx:end_sample]

                        if len(audio_chunk) > 0:
                            # OPTIMIZATION: Ensure contiguous array and correct dtype upfront
                            audio_chunk_f32 = np.ascontiguousarray(audio_chunk.T, dtype=np.float32)

                            audio_frame = av.AudioFrame.from_ndarray(
                                audio_chunk_f32, format='fltp', layout='stereo'
                            )
                            audio_frame.sample_rate = audio_sample_rate
                            audio_frame.pts = sample_idx  # PTS is relative to the start of the trimmed audio
                            audio_frame.time_base = mic_audio_stream.time_base

                            for packet in mic_audio_stream.encode(audio_frame):
                                container.mux(packet)

                # Process system audio stream
                if sys_audio_stream and sys_data is not None:
                    for sample_idx in range(0, total_samples_sys, chunk_size):
                        end_sample = min(sample_idx + chunk_size, total_samples_sys)
                        audio_chunk = sys_data[sample_idx:end_sample]

                        if len(audio_chunk) > 0:
                            # OPTIMIZATION: Ensure contiguous array and correct dtype upfront
                            audio_chunk_f32 = np.ascontiguousarray(audio_chunk.T, dtype=np.float32)

                            audio_frame = av.AudioFrame.from_ndarray(
                                audio_chunk_f32, format='fltp', layout='stereo'
                            )
                            audio_frame.sample_rate = audio_sample_rate
                            audio_frame.pts = sample_idx  # PTS is relative to the start of the trimmed audio
                            audio_frame.time_base = sys_audio_stream.time_base

                            for packet in sys_audio_stream.encode(audio_frame):
                                container.mux(packet)
            else:
                # Original combined audio processing
                if combined_audio is not None:
                    for sample_idx in range(0, total_samples, chunk_size):  # total_samples is already adjusted
                        end_sample = min(sample_idx + chunk_size, total_samples)
                        audio_chunk = combined_audio[sample_idx:end_sample]

                        if len(audio_chunk) > 0:
                            # OPTIMIZATION: Ensure contiguous array and correct dtype upfront
                            audio_chunk_f32 = np.ascontiguousarray(audio_chunk.T, dtype=np.float32)

                            audio_frame = av.AudioFrame.from_ndarray(
                                audio_chunk_f32, format='fltp', layout='stereo'
                            )
                            audio_frame.sample_rate = audio_sample_rate
                            audio_frame.pts = sample_idx  # PTS is relative to the start of the trimmed audio
                            audio_frame.time_base = audio_stream.time_base

                            for packet in audio_stream.encode(audio_frame):
                                container.mux(packet)

        # Flush encoders
        # print("Finalizing...")

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