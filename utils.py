from video import VID_record
from mic_audio import MIC_record
from sys_audio import SYS_record
from encoder import encode_streams
#from status import notif_bridge

#notif_bridge = None

import threading, time, os, sys
from datetime import datetime


class ThreadWithReturnValue(threading.Thread):
    def __init__(self, group=None, target=None, name=None, args=(), kwargs={}):
        super().__init__(group, target, name, args, kwargs)
        self._return = None

    def run(self):
        if self._target is not None:
            self._return = self._target(*self._args, **self._kwargs)

    # A custom 'join' that also returns the value
    def join(self, *args):
        super().join(*args)
        return self._return


class StreamController:
    def __init__(self):
        self.vid_recorder = VID_record()
        self.mic_recorder = MIC_record()
        self.sys_recorder = SYS_record()
        self.enabled_streams = [True, True, True]
        self._streams = [self.vid_recorder, self.mic_recorder, self.sys_recorder]
        self.streams = [self.vid_recorder, self.mic_recorder, self.sys_recorder]
        self.buffers_live = False

        self.was_buffers_dead = False

        self.recording = False

        self.output_directory = 'clips'
        self.set_output_directory(self.output_directory)

        self.clip_duration = 5
        self._minimum_extra_buffer_duration = 10
        self.buffer_duration = 0
        self.set_clip_duration(self.clip_duration)

        self.clip_fps = 60
        self.set_video_fps(self.clip_fps)

        self.audio_sample_rate = 44100
        self.set_audio_sample_rate(self.audio_sample_rate)

        self.bitrate = 5000000
        self.set_bitrate(self.bitrate)

        self.last_clip_time = 0
        self.recording_start_time = 0

        self.clip_timeout_seconds = 1
        self.in_timeout = False

        self.timestamps = []

        self.notification_stay_ms = 4000

        self.notification_object = None
        #t = ThreadWithReturnValue(target=self.mic_recorder.get_microphone_device)
        #t.start()
        #self.mic_default, self.mics = t.join()
        self.mic_default, self.mics = self.mic_recorder.get_microphone_device()
        self.mic_default = self.mic_default.name
        self.mics = [i.name for i in self.mics]
        self.unload_soundcard()

        self.split_audio = False

    def enable_stream_vid(self):
        print('!enable_stream_vid')
        if self.enabled_streams[0]:
            return
        self.enabled_streams[0] = True
        self.update_enabled_streams()

    def disable_stream_vid(self):
        print('!disable_stream_vid')
        if not self.enabled_streams[0]:
            return
        self.enabled_streams[0] = False
        self.update_enabled_streams()

    def enable_stream_mic(self):
        print('!enable_stream_mic')
        if self.enabled_streams[1]:
            return
        self.enabled_streams[1] = True
        self.update_enabled_streams()

    def disable_stream_mic(self):
        print('!disable_stream_mic')
        if not self.enabled_streams[1]:
            return
        self.enabled_streams[1] = False
        self.update_enabled_streams()

    def enable_stream_sys(self):
        print('!enable_stream_sys')
        if self.enabled_streams[2]:
            return
        self.enabled_streams[2] = True
        self.update_enabled_streams()

    def disable_stream_sys(self):
        print('!disable_stream_sys')
        if not self.enabled_streams[2]:
            return
        self.enabled_streams[2] = False
        self.update_enabled_streams()

    def update_enabled_streams(self):
        was_recording = self.buffers_live

        if self.buffers_live:
            self.stop()
        self.streams = []
        for a, b in zip(self.enabled_streams, self._streams):
            if a:
                self.streams.append(b)
        if was_recording:
            self.start()

    def set_clip_duration(self, duration):
        print(f'!set_clip_duration {duration}')
        self.clip_duration = duration
        if self.recording:
            return
        else:
            self.set_buffer_duration(self.clip_duration)

    def set_buffer_duration(self, duration):
        self.buffer_duration = duration
        for stream in self.streams:
            stream.clip_duration = duration + self._minimum_extra_buffer_duration

    def set_video_fps(self, fps):
        print(f'!set_video_fps {fps}')
        was_recording = self.buffers_live

        if self.buffers_live:
            self.stop()

        self.clip_fps = fps
        self._streams[0].fps = fps

        if was_recording:
            self.start()

    def set_audio_sample_rate(self, sample_rate):
        print(f'!set_audio_sample_rate {sample_rate}')
        was_recording = self.buffers_live

        if self.buffers_live:
            self.stop()

        self.audio_sample_rate = sample_rate
        self._streams[1].SAMPLE_RATE = sample_rate
        self._streams[2].SAMPLE_RATE = sample_rate

        if was_recording:
            self.start()

    def set_bitrate(self, bitrate):
        print(f'!set_bitrate {bitrate}')
        self.bitrate = bitrate

        was_recording = self.buffers_live

        if self.buffers_live:
            self.stop()

        self._streams[0].bitrate = self.bitrate

        if was_recording:
            self.start()

    def set_output_directory(self, dirr):
        print(f'!set_output_directory {dirr}')
        self.output_directory = dirr
        os.makedirs(dirr, exist_ok=True)

    def set_mic(self, mic):
        print(f'!set_mic {mic}')

        was_recording = self.buffers_live

        if self.buffers_live:
            self.stop()

        # `mic` is a device name from list_mics() (or None / the default name).
        # Store None when it's the default so the recorder just resolves the
        # system default; store the explicit name otherwise.
        if mic and mic != self.mic_default:
            self.mic_recorder.selected_mic_name = mic
        else:
            self.mic_recorder.selected_mic_name = None

        if was_recording:
            self.start()

    def list_mics(self):
        print('!list_mics')
        return self.mics

    def default_mic(self):
        print('!default_mic')
        return self.mic_default

    def enable_record_mouse(self):
        print('!enable_record_mouse')
        self.vid_recorder.record_mouse = True

    def disable_record_mouse(self):
        print('!disable_record_mouse')
        self.vid_recorder.record_mouse = False

    def set_resolution(self, resolution):
        print(f'!set_resolution {resolution}')

        was_recording = self.buffers_live

        if self.buffers_live:
            self.stop()

        self.vid_recorder.target_res = resolution

        if was_recording:
            self.start()

    def start(self, show=False):
        print('!start')
        if show:
            self.notification_object(self.notification_stay_ms, f'Clipping Enabled, Press Hotkey to Clip', 'INSTANT REPLAY ENABLED')
        self.was_buffers_dead = False
        if self.buffers_live:
            return
        self.buffers_live = True
        threads = []
        for each in self.streams:
            t = threading.Thread(target=each.start)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        for stream in self.streams:  # sync buffers just because
            stream.reset_buffer()

    def stop(self, show=False):
        print('!stop')
        if show:
            self.notification_object(self.notification_stay_ms, f'Clipping Disabled', 'INSTANT REPLAY DISABLED')
        self.was_buffers_dead = False
        if not self.buffers_live:
            return
        self.buffers_live = False
        threads = []
        for each in self.streams:
            t = threading.Thread(target=each.stop)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        self.unload_soundcard()

    def temporal_crop(self, data, cutoff):
        return [i for i in data if i[1] > cutoff]

    def write_streams(self, streams):
        timestamp = datetime.now().strftime("%Y_%m_%d-%H_%M_%S")
        if timestamp in self.timestamps:
            timestamp = f'{timestamp}-2'
        self.timestamps.append(timestamp)
        self.timestamps = self.timestamps[-10:]
        for enabled in self.enabled_streams:
            if enabled:
                streams.append(streams.pop(0))
            else:
                streams.append(None)

        encode_streams(os.path.join(self.output_directory, f"clip_or_recording_{timestamp}"), streams[0], streams[1], streams[2],
                       video_fps=self.clip_fps, audio_sample_rate=self.audio_sample_rate, discard_from_beginning=self.diff, split_audio_tracks=self.split_audio)

    def get_stream_data(self, cutoff_time):
        self._temp_stream_array = []

        def atom(stream, index):
            data = stream.clip((cutoff_time))
            self._temp_stream_array.append((data, index))

        ts = [threading.Thread(target=atom, args=(stream,i)) for i, stream in enumerate(self.streams)]

        for t in ts:
            t.start()

        for t in ts:
            t.join()

        streams = sorted(self._temp_stream_array, key=lambda x: x[1])

        streams = [i[0] for i in streams]

        min_frames = 2
        frame_count = min_frames + 1
        overrun = False
        self.diff = 0
        for stream in streams:
            if len(stream[0]) == 4:
                iframe_times = []
                for frame in stream:
                    if frame[3]:
                        iframe_times.append(frame[1])
                    if frame[1] > cutoff_time:
                        if iframe_times:
                            break
                        else:
                            overrun = True

                frame_count = len(stream)

                if not overrun:
                    self.diff = cutoff_time-iframe_times[-1]
                print(f'Overrun: {overrun}\nTotal iframes: {len(iframe_times)}\niframe diff: {self.diff}\nframes: {frame_count}\nfps: {frame_count/(stream[-1][1]-stream[0][1])}')
                cutoff_time = iframe_times[-1]
                cutoff_time -= 1/1000

        if frame_count < min_frames:
            return False

        streams = [self.temporal_crop(stream, cutoff_time) for stream in streams]
        return streams

    def format_seconds(self, sec):
        d, sec = divmod(sec, 86400)
        h, sec = divmod(sec, 3600)
        m, s = divmod(sec, 60)
        out = []
        if d: out.append(f"{d} day{'s' * (d != 1)}")
        if h: out.append(f"{h} hour{'s' * (h != 1)}")
        if m: out.append(f"{m} minute{'s' * (m != 1)}")
        if s or not out: out.append(f"{s} second{'s' * (s != 1)}")
        return " ".join(out)

    def wait_clip_timeout(self):
        if time.time()-self.last_clip_time < self.clip_timeout_seconds:
            self.in_timeout = True
            sleep_amount = self.last_clip_time+self.clip_timeout_seconds-time.time()
            print(sleep_amount)
            if sleep_amount > 0:
                time.sleep(sleep_amount)
            self.in_timeout = False

    def clip(self):
        print('!clip')
        threading.Thread(target=self._clip).start()

    def _clip(self):
        #if self.in_timeout:
        #    return

        #self.wait_clip_timeout()

        lct = time.time()

        clipped_from = max(self.last_clip_time, time.time()-self.clip_duration)

        streams = self.get_stream_data(clipped_from)

        if not streams:
            return

        self.notification_object(self.notification_stay_ms, f'Clipped last {self.format_seconds(round(time.time() - clipped_from))}', 'INSTANT REPLAY')

        self.last_clip_time = lct

        self.write_streams(streams)

    def start_recording(self):
        print('!start_recording')
        if not self.buffers_live:
            self.start()
            self.was_buffers_dead = True
        self.notification_object(self.notification_stay_ms, f'Recording started', 'RECORDING')
        self.recording_start_time = time.time()
        self.recording = True
        self.set_buffer_duration(2 ** 32) # 136 years, basically just unlock the buffer size

    def stop_recording(self):
        print('!stop_recording')
        threading.Thread(target=self._stop_recording).start()

    def _stop_recording(self):
        self.recording = False
        self.notification_object(self.notification_stay_ms, f'Recording saved, last {self.format_seconds(round(time.time()-self.recording_start_time))}', 'RECORDING')
        streams = self.get_stream_data(self.recording_start_time)
        if self.was_buffers_dead:
            self.stop()
        self.set_buffer_duration(self.clip_duration)
        self.write_streams(streams)

    def unload_soundcard(self):
        for modname in list(sys.modules):
            if modname == "soundcard" or modname.startswith("soundcard."):
                del sys.modules[modname]