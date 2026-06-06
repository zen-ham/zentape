import time
import numpy as np
import threading
import kthread
import soundfile as sf
import warnings


class SYS_record:
    def __init__(self):
        self.SAMPLE_RATE = 44100
        self.CHANNELS = 2
        self.clip_duration = 5
        self.clip_directory = "clips"

        self.audio_data_chunks = []  # List to store individual audio chunks (numpy arrays)
        self.stop_event = threading.Event()

        self.recording_thread = None

        self.name = 'system_audio'
        self.ext = '.wav'

    def get_loopback_device(self):
        import soundcard as sc
        from soundcard.mediafoundation import SoundcardRuntimeWarning

        # Mute the specific warning
        warnings.filterwarnings("ignore", category=SoundcardRuntimeWarning)
        try:
            default_speaker = sc.default_speaker()
            loopback_mic = sc.get_microphone(id=default_speaker.id, include_loopback=True)
            print(f"Using default speaker's loopback: {loopback_mic.name} (ID: {loopback_mic.id})")
            return loopback_mic
        except Exception as e:
            print(f"Could not get default speaker as loopback microphone: {e}")
            print("Trying to find a loopback device by name...")
            for mic in sc.all_microphones(include_loopback=True):
                name_lower = mic.name.lower()
                if "stereo mix" in name_lower or \
                        "what u hear" in name_lower or \
                        "monitor" in name_lower or \
                        "loopback" in name_lower:
                    print(f"Found potential loopback device: {mic.name} (ID: {mic.id})")
                    return mic
            print("No suitable loopback device found. Please ensure a 'Stereo Mix' or similar device is enabled in your sound settings.")
            return None

    def continuous_record_buffer(self):
        loopback_mic = self.get_loopback_device()
        if loopback_mic is None:
            print("Failed to initialize audio capture. Exiting recording thread.")
            self.stop_event.set()
            return

        buffer_max_samples = self.clip_duration * self.SAMPLE_RATE
        chunks_per_second = 60
        chunk_size = self.SAMPLE_RATE // chunks_per_second

        #print(f"Recording system audio into a {self.clip_duration}-second buffer (Press F8 to save)...")
        #print(f"Sample Rate: {self.SAMPLE_RATE} Hz, Channels: {self.CHANNELS}")
        #print(f"Max Buffer Size (samples): {buffer_max_samples}")

        with loopback_mic.recorder(samplerate=self.SAMPLE_RATE, channels=self.CHANNELS) as mic:
            while not self.stop_event.is_set():
                buffer_max_samples = self.clip_duration * self.SAMPLE_RATE
                try:
                    chunk = mic.record(numframes=chunk_size)

                    # Ensure the chunk has the correct number of channels
                    if chunk.ndim == 1:
                        chunk = np.expand_dims(chunk, axis=1)
                        if self.CHANNELS == 2:
                            chunk = np.hstack((chunk, chunk))
                    elif chunk.shape[1] > self.CHANNELS:
                        chunk = chunk[:, :self.CHANNELS]

                    self.audio_data_chunks.append((chunk, time.time()))

                    # Trim from the beginning if buffer exceeds max duration
                    if self.audio_data_chunks:
                        while time.time() - self.audio_data_chunks[0][1] > self.clip_duration:
                            self.audio_data_chunks.pop(0)

                except Exception as e:
                    print(f"Error during recording: {e}")
                    time.sleep(5)
        print("Recording buffer stopped.")

    def reset_buffer(self):
        self.audio_data_chunks = []

    def clip(self, args=None):
        return self.audio_data_chunks

    def start(self):
        if self.recording_thread and self.recording_thread.is_alive():
            print("Recording is already running.")
            return
        self.stop_event.clear()
        self.recording_thread = kthread.KThread(target=self.continuous_record_buffer, daemon=True)
        self.recording_thread.start()
        #print("System audio recording started.")

    def stop(self):
        if self.recording_thread is None or not self.recording_thread.is_alive():
            print("Recording is not active.")
            return

        self.stop_event.set()
        self.recording_thread.join(timeout=1)
        if self.recording_thread.is_alive():
            print("Warning: Recording thread did not terminate gracefully")
            self.recording_thread.kill()
            print('^attempted termination')
        self.recording_thread = None
        print("System audio recording stopped.")
        self.reset_buffer()

    def write_file(self, data, filename):
        data = [i[0] for i in data]
        data_to_save = np.concatenate(data, axis=0)

        try:
            sf.write(file=filename, data=data_to_save, samplerate=self.SAMPLE_RATE)
            print(f"Saved {data_to_save.shape[0] / self.SAMPLE_RATE:.2f}-second audio clip to {filename}")
        except Exception as e:
            print(f"Error saving audio file: {e}")