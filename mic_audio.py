import os
import time
import soundfile as sf
import numpy as np
import threading
import kthread # Used for killable threads


class MIC_record: # Renamed the class for clarity
    def __init__(self):
        self.SAMPLE_RATE = 44100
        self.CHANNELS = 2 # Keeping 2 channels as in original, even if mic is mono, it will be duplicated
        self.clip_duration = 5
        self.clip_directory = "clips"
        self.last_clip_time = 0

        self.audio_data_chunks = []  # List to store individual audio chunks (numpy arrays)
        self.stop_event = threading.Event()

        self.recording_thread = None

        self.name = 'microphone_audio'
        self.ext = '.wav'

        # Name of the mic the user picked in the GUI. None => use system default.
        self.selected_mic_name = None


    def get_microphone_device(self): # Renamed and simplified
        """
        Attempts to get the default microphone. If that fails, tries to get
        the default input device, or the first available microphone.
        """
        import soundcard as sc
        default = None
        try:
            default = sc.default_microphone()
            print(f"Using default microphone: {default.name} (ID: {default.id})")
        except Exception as e:
            print(f"Could not get default microphone: {e}")

        all_mics = sc.all_microphones()
        if all_mics and not default:
            default = all_mics[0]
            print(f"Falling back to first available microphone: {default.name} (ID: {default.id})")
            return default, all_mics
        elif all_mics and default:
            return default, all_mics
        elif default and not all_mics:
            return default, [default]
        else:
            print("No microphone devices found. Please ensure a microphone is connected and enabled.")
            return None


    def _resolve_microphone(self):
        """Resolve the device to actually open: the user-selected mic if one is set
        and still present, otherwise the system default."""
        import soundcard as sc
        name = self.selected_mic_name
        if name:
            try:
                for m in sc.all_microphones():
                    if m.name == name:
                        print(f"Using selected microphone: {m.name} (ID: {m.id})")
                        return m
                print(f"Selected microphone '{name}' not found; falling back to default.")
            except Exception as e:
                print(f"Error resolving selected microphone '{name}': {e}")
        return self.get_microphone_device()[0]

    def continuous_record_buffer(self):
        microphone = self._resolve_microphone()
        if microphone is None:
            print("Failed to initialize microphone capture. Exiting recording thread.")
            self.stop_event.set()
            return

        buffer_max_samples = self.clip_duration * self.SAMPLE_RATE
        # Using a smaller chunk size for smoother buffer updates, same as original
        chunks_per_second = 60
        chunk_size = self.SAMPLE_RATE // chunks_per_second

        #print(f"Recording microphone audio into a {self.clip_duration}-second buffer (Press F8 to save)...")
        #print(f"Sample Rate: {self.SAMPLE_RATE} Hz, Channels: {self.CHANNELS} (Note: Mono mics will be duplicated to stereo)")
        #print(f"Max Buffer Size (samples): {buffer_max_samples}")

        try:
            # Use the selected microphone device
            with microphone.recorder(samplerate=self.SAMPLE_RATE, channels=self.CHANNELS) as mic:
                while not self.stop_event.is_set():
                    buffer_max_samples = self.clip_duration * self.SAMPLE_RATE
                    try:
                        chunk = mic.record(numframes=chunk_size)

                        # Ensure the chunk has the correct number of channels
                        # This handles mono microphones (1D or Nx1) by expanding to 2 channels (Nx2)
                        if chunk.ndim == 1:
                            chunk = np.expand_dims(chunk, axis=1) # Make it Nx1
                            if self.CHANNELS == 2:
                                chunk = np.hstack((chunk, chunk)) # Duplicate for stereo
                        elif chunk.shape[1] > self.CHANNELS:
                            chunk = chunk[:, :self.CHANNELS] # Trim if too many channels

                        self.audio_data_chunks.append((chunk, time.time()))

                        # Trim from the beginning if buffer exceeds max duration
                        if self.audio_data_chunks:
                            while time.time() - self.audio_data_chunks[0][1] > self.clip_duration:
                                self.audio_data_chunks.pop(0)

                    except Exception as e:
                        print(f"Error during microphone recording: {e}")
                        time.sleep(1) # Small delay before trying again to prevent rapid error spam
        except Exception as e:
            print(f"Failed to open microphone recorder: {e}")
            self.stop_event.set() # Stop the thread if recorder cannot be opened
        print("Microphone recording buffer stopped.")

    def reset_buffer(self):
        self.audio_data_chunks = []

    def clip(self, args=None):
        return self.audio_data_chunks

    def start(self):
        if self.recording_thread and self.recording_thread.is_alive():
            print("Microphone recording is already running.")
            return
        self.stop_event.clear()
        # kthread is used here for its kill() method, useful for forceful termination if needed
        self.recording_thread = kthread.KThread(target=self.continuous_record_buffer, daemon=True)
        self.recording_thread.start()
        #print("Microphone audio recording started.")

    def stop(self):
        if self.recording_thread is None or not self.recording_thread.is_alive():
            print("Microphone recording is not active.")
            return

        self.stop_event.set()
        # Give the thread a moment to shut down gracefully
        self.recording_thread.join(timeout=2) # Increased timeout slightly
        if self.recording_thread.is_alive():
            print("Warning: Microphone recording thread did not terminate gracefully.")
            # Forcefully kill the thread if it's still alive
            self.recording_thread.kill()
            print('^Attempted forceful termination.')
        self.recording_thread = None
        print("Microphone audio recording stopped.")
        self.reset_buffer()

    def write_file(self, data, filename):
        data = [i[0] for i in data]
        data_to_save = np.concatenate(data, axis=0)

        try:
            sf.write(file=filename, data=data_to_save, samplerate=self.SAMPLE_RATE)
            print(f"Saved {data_to_save.shape[0] / self.SAMPLE_RATE:.2f}-second audio clip to {filename}")
        except Exception as e:
            print(f"Error saving audio file: {e}")
