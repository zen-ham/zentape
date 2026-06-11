import cv2
import time
#from ffmpeg_screenshot_pipe import FFmpegshot
import threading
import os
import subprocess
import av
from fractions import Fraction
import queue
import bettercam

# OpenCV defaults to multi-threading each call, which oversubscribes cores when
# we also run a worker pool (CPU fallback path); our pool is the parallelism.
try:
    cv2.setNumThreads(1)
except Exception:
    pass


import ctypes
from ctypes import wintypes
import win32gui
import win32ui
import win32con
from PIL import Image
import numpy as np  # Import NumPy

# --- Make the process DPI-aware (MUST be called before any other UI calls) ---
try:
    # Use PROCESS_PER_MONITOR_DPI_AWARE_V2 for best results on modern systems
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    # Fallback for older systems
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(wintypes.HANDLE(-2))  # PROCESS_SYSTEM_DPI_AWARE
    except AttributeError:
        ctypes.windll.user32.SetProcessDPIAware()


# --- Define necessary C structures ---
class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HICON),
                ("ptScreenPos", wintypes.POINT)]


class ICONINFO(ctypes.Structure):
    _fields_ = [('fIcon', wintypes.BOOL),
                ('xHotspot', wintypes.DWORD),
                ('yHotspot', wintypes.DWORD),
                ('hbmMask', wintypes.HBITMAP),
                ('hbmColor', wintypes.HBITMAP)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", wintypes.LONG),
                ("bmWidth", wintypes.LONG),
                ("bmHeight", wintypes.LONG),
                ("bmWidthBytes", wintypes.LONG),
                ("bmPlanes", wintypes.WORD),
                ("bmBitsPixel", wintypes.WORD),
                ("bmBits", wintypes.LPVOID)]


# --- Define function prototypes for ctypes ---
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
user32.GetCursorInfo.restype = wintypes.BOOL

user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]
user32.GetIconInfo.restype = wintypes.BOOL

gdi32.GetObjectW.argtypes = [wintypes.HANDLE, wintypes.INT, ctypes.c_void_p]
gdi32.GetObjectW.restype = wintypes.INT

gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL


# --- Main Logic ---
# Cache of extracted cursor bitmaps keyed by HCURSOR handle, so the expensive
# GDI redraw only happens when the cursor shape actually changes.
# (Trade-off: animated cursors that reuse one handle will show a static frame.)
_cursor_bitmap_cache = {}


def capture_cursor():
    """
    Captures the cursor image, its position, and hotspot.

    Returns:
        A tuple of (numpy.ndarray, tuple, tuple, bool) representing:
        - The cursor image as an RGBA NumPy array (height, width, 4).
        - The cursor's screen position (x, y).
        - The cursor's hotspot (x, y).
        - A boolean, True if the cursor is monochrome/inverting (like an I-beam).
        Returns (None, None, None, None) if the cursor is not visible or an error occurs.
    """

    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(ci)
    if not user32.GetCursorInfo(ctypes.byref(ci)):
        return

    if ci.flags != win32con.CURSOR_SHOWING:
        return

    h_cursor = ci.hCursor
    cursor_pos = (ci.ptScreenPos.x, ci.ptScreenPos.y)

    # Bitmap depends only on the cursor handle; position is the only thing that
    # changes most frames. Reuse the cached extraction when the shape is the same.
    cached = _cursor_bitmap_cache.get(h_cursor)
    if cached is not None:
        rgba_array, hotspot, is_monochrome = cached
        return rgba_array, cursor_pos, hotspot, is_monochrome

    icon_info = ICONINFO()

    try:
        if not user32.GetIconInfo(h_cursor, ctypes.byref(icon_info)):
            return

        is_monochrome = not icon_info.hbmColor

        hotspot = (icon_info.xHotspot, icon_info.yHotspot)

        if is_monochrome:
            mask_bitmap_info = BITMAP()
            gdi32.GetObjectW(icon_info.hbmMask, ctypes.sizeof(mask_bitmap_info), ctypes.byref(mask_bitmap_info))
            width = mask_bitmap_info.bmWidth
            height = mask_bitmap_info.bmHeight // 2
        else:
            color_bitmap_info = BITMAP()
            gdi32.GetObjectW(icon_info.hbmColor, ctypes.sizeof(color_bitmap_info), ctypes.byref(color_bitmap_info))
            width = color_bitmap_info.bmWidth
            height = color_bitmap_info.bmHeight

        if width == 0 or height == 0:
            return

        hdc = win32ui.CreateDCFromHandle(win32gui.GetDC(0))
        mem_dc = hdc.CreateCompatibleDC()

        save_bitmap = win32ui.CreateBitmap()
        save_bitmap.CreateCompatibleBitmap(hdc, width, height)
        mem_dc.SelectObject(save_bitmap)

        if is_monochrome:
            white_brush = win32gui.GetStockObject(win32con.WHITE_BRUSH)
            win32gui.FillRect(mem_dc.GetSafeHdc(), (0, 0, width, height), white_brush)

        try:
            win32gui.DrawIconEx(mem_dc.GetSafeHdc(), 0, 0, h_cursor, 0, 0, 0, None, 0x0003)
        except:
            return

        bmp_info = save_bitmap.GetInfo()
        bmp_str = save_bitmap.GetBitmapBits(True)
        pil_img = Image.frombuffer('RGBA', (bmp_info['bmWidth'], bmp_info['bmHeight']), bmp_str, 'raw', 'BGRA', 0, 1)

        if is_monochrome:
            final_img = Image.new("RGBA", (width, height))
            mask = pil_img.convert("L").point(lambda p: 255 if p < 255 else 0)
            final_img.paste((0, 0, 0, 255), mask=mask)
            pil_img = final_img

        win32gui.ReleaseDC(0, hdc.GetSafeHdc())
        mem_dc.DeleteDC()
        win32gui.DeleteObject(save_bitmap.GetHandle())

        # --- CONVERT TO NUMPY ARRAY AND RETURN ---
        rgba_array = np.array(pil_img)
        _cursor_bitmap_cache[h_cursor] = (rgba_array, hotspot, is_monochrome)
        return rgba_array, cursor_pos, hotspot, is_monochrome

    finally:
        if icon_info.hbmColor:
            gdi32.DeleteObject(icon_info.hbmColor)
        if icon_info.hbmMask:
            gdi32.DeleteObject(icon_info.hbmMask)


def overlay_cursor_on_screenshot(screenshot, cursor_rgba, cursor_pos, is_inverting):
    """Overlay cursor on screenshot numpy array"""
    cursor_h, cursor_w = cursor_rgba.shape[:2]
    screen_h, screen_w = screenshot.shape[:2]

    # Calculate placement bounds
    x, y = cursor_pos

    # Clamp cursor position to screen bounds
    x_start = max(0, x)
    y_start = max(0, y)
    x_end = min(screen_w, x + cursor_w)
    y_end = min(screen_h, y + cursor_h)

    # Calculate cursor slice bounds
    cursor_x_start = max(0, -x)
    cursor_y_start = max(0, -y)
    cursor_x_end = cursor_x_start + (x_end - x_start)
    cursor_y_end = cursor_y_start + (y_end - y_start)

    if x_end <= x_start or y_end <= y_start:
        return  # Cursor completely outside screen

    # Get the regions to blend
    screen_region = screenshot[y_start:y_end, x_start:x_end]
    cursor_region = cursor_rgba[cursor_y_start:cursor_y_end, cursor_x_start:cursor_x_end]

    # --- NEW LOGIC: Check if the cursor should invert colors ---
    if is_inverting:
        # Create a mask from the cursor's alpha channel
        cursor_mask = cursor_region[:, :, 3] > 0

        # Invert the screen region pixels where the cursor is opaque
        # We use a temporary copy to perform the inversion
        inverted_region = screen_region.copy()
        inverted_region[cursor_mask] = 255 - inverted_region[cursor_mask]

        # Update the original screenshot with the inverted pixels
        screenshot[y_start:y_end, x_start:x_end] = inverted_region
    else:
        # --- ORIGINAL LOGIC: Alpha blending for normal cursors ---
        alpha = cursor_region[:, :, 3:4] / 255.0

        # Convert screen region to same format (add alpha channel if needed)
        if screen_region.shape[2] == 3:  # BGR to BGRA
            screen_region_rgba = np.concatenate([screen_region, np.full((*screen_region.shape[:2], 1), 255, dtype=np.uint8)], axis=2)
        else:
            screen_region_rgba = screen_region.copy()

        # Blend cursor with screen
        blended = screen_region_rgba[:, :, :3] * (1 - alpha) + cursor_region[:, :, :3] * alpha

        # Update the screenshot
        screenshot[y_start:y_end, x_start:x_end, :3] = blended.astype(np.uint8)


def composite_cursor_dpi_aware(screenshot, info):
    # Get cursor data
    # --- MODIFIED: Unpack the new is_inverting flag ---
    cursor_rgba, pos_win, (hotspotx, hotspoty), is_inverting = info

    if cursor_rgba is None:
        print("Failed to capture cursor")
        return

    # Get cursor position and apply scaling
    ratio = ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100
    # pos_win = win32gui.GetCursorPos()
    pos = (round(pos_win[0] * ratio - hotspotx), round(pos_win[1] * ratio - hotspoty))

    # Overlay cursor on screenshot
    # --- MODIFIED: Pass the new is_inverting flag ---
    overlay_cursor_on_screenshot(screenshot, cursor_rgba, pos, is_inverting)


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


def calculate_scaled_resolution(original_width, original_height, target_height=None, target_width=None):
    aspect_ratio = original_width / original_height

    if target_height is not None:
        new_height = target_height
        new_width = round(aspect_ratio * new_height)
    else:  # target_width is not None
        new_width = target_width
        new_height = round(new_width / aspect_ratio)

    # Ensure dimensions are at least 1, though with positive inputs this is usually implicit
    new_width = max(1, int(new_width))
    new_height = max(1, int(new_height))

    return new_width, new_height


def get_primary_monitor_fps():
    """Best-effort current refresh rate (Hz) of the primary monitor."""
    try:
        import win32api, win32con
        dm = win32api.EnumDisplaySettings(None, win32con.ENUM_CURRENT_SETTINGS)
        hz = float(dm.DisplayFrequency)
        # Windows reports 0 or 1 for "hardware default" / unknown.
        if hz > 1:
            return hz
    except Exception as e:
        print(f'Could not detect monitor refresh rate: {e}')
    return 60.0


def _primary_resolution():
    """Native pixel resolution of the primary monitor (process is DPI-aware)."""
    try:
        u = ctypes.windll.user32
        return int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
    except Exception:
        return 1920, 1080


class _PipeReader:
    """Read-only, non-seekable file-like wrapper around a subprocess pipe so PyAV
    treats it as a pure stream and never tries to seek() it (which fails on a
    pipe with EINVAL)."""
    def __init__(self, f):
        self._f = f

    def read(self, n):
        return self._f.read(n)


class _StreamParams:
    """Duck-typed stand-in for a source codec/stream that encoder.encode_streams
    reads (name/width/height/pix_fmt/time_base/options). For GPU clips we use the
    plain codec name 'h264' so add_stream() creates a stream-copy target rather
    than opening another NVENC encoder session."""
    def __init__(self, name, width, height, pix_fmt, time_base, options=None):
        self.name = name
        self.width = width
        self.height = height
        self.pix_fmt = pix_fmt
        self.time_base = time_base
        self.options = options or {}


class VID_record:
    def __init__(self):
        self._is_recording = False
        self._recording_thread = None
        self.frames_buffer = []  # Stores a rolling buffer of frames
        self.fps = 60  # Default Frames Per Second
        self.monitor_index = 0  # Default monitor index (primary display)
        self.clip_duration = 5  # Duration for the clip function
        self.clip_directory = 'clips'
        self.name = 'screen_recording'
        self.ext = '.mp4'

        self.record_ffmpeg = False

        self.encode = True

        self.record_mouse = True

        self.target_res = '720'

        self.bitrate = 5000000

        # Parallel preprocessing (cursor composite + colour convert + Lanczos
        # resize) is the CPU-heavy, single-threaded bottleneck, so fan it out
        # across a pool while keeping the encode step ordered and serial. Default
        # leaves plenty of cores free for whatever is being recorded.
        self.num_preprocess_workers = max(2, min(6, (os.cpu_count() or 8) - 8))

        self.frame_q = queue.Queue(maxsize=max(8, self.num_preprocess_workers * 3))

        # Signalled by the encode worker every time a frame lands in the buffer,
        # so clip() can wait efficiently instead of busy-spinning.
        self._buffer_cond = threading.Condition()

        # Reorder/handoff state between the preprocess pool and the encode
        # thread (initialised per-session in start()).
        self._results = {}
        self._results_cond = threading.Condition()
        self._capture_seq = 0
        self._next_encode_seq = 0
        self._final_seq = None
        self._preprocess_threads = []
        self._encode_thread = None

        # GPU-pipeline (primary path) state. Capture+scale+encode run entirely on
        # the GPU via an ffmpeg ddagrab->scale_d3d11->h264_nvenc subprocess; we
        # only demux the resulting packets into the ring buffer (~0% CPU). Falls
        # back to the bettercam+cv2 path above when the GPU pipeline isn't usable.
        self._mode = None              # 'gpu' or 'cpu' for the active session
        self._ffmpeg_proc = None
        self._gpu_thread = None
        self._gpu_codec = None         # codec param descriptor used by the clip muxer
        # CPU path can re-encode the sub-keyframe head for an exact clip start;
        # the GPU path keyframe-snaps instead (a 2nd/3rd NVENC session for the
        # head re-encode isn't reliable alongside the capture pipeline, and with
        # a ~1s GOP the snap is within a second of the requested point).
        self.supports_precise_clip = True
        # GPU pipeline (ddagrab->nvenc->mpegts->pipe) delays each frame from
        # capture to when we demux it by a roughly constant latency; the
        # separately-captured audio is stamped at real time, so without this the
        # audio leads the video. Subtracted from GPU video stamps. Value measured
        # by perf_lab/av_sync_measure.py (flash + 19kHz tone): audio led by
        # 0.997s +/- 6ms. Tune: if audio still LEADS, increase; if it LAGS, decrease.
        self.gpu_av_latency = 0.997

    def _record_loop(self):
        self.recording_pts = 0
        self.encoding_pts = 0
        self.dropped_frames = 0
        if self.record_ffmpeg:
            with FFmpegshot() as sc:
                for screenshot in sc.capture_one_screen_ddagrab(
                #for screenshot in sc.capture_one_screen_gdigrab(
                        monitor_index=self.monitor_index,
                        frames=self.fps,
                        draw_mouse=True,
                ):
                    if not self._is_recording:
                        break

                    if frame_q.full():
                        print('warning: frame queue is full, recorder will wait...')

                    self.frame_q.put((screenshot, time.time()))

                    self.recording_pts += 1
        else:
            try: # try gpu then default
                bc = bettercam.create(device_idx=self.monitor_index, output_idx=0, output_color="BGR", max_buffer_len=64)
                print('Recording using GPU')
            except:
                bc = bettercam.create(device_idx=self.monitor_index, output_color="BGR", max_buffer_len=64)
                print('Recording not able to use GPU, falling back to CPU')


            bc.start(target_fps=self.fps, video_mode=True)

            while True:
                if not self._is_recording:
                    break

                screenshot = bc.get_latest_frame()
                if screenshot is None:
                    continue

                # Cursor capture is cheap now (bitmap cached by handle), so do it
                # inline rather than spawning a fresh thread every frame.
                info = capture_cursor() if self.record_mouse else None

                try:
                    # Non-blocking: if the pool is saturated, drop this frame
                    # rather than stalling capture. With wall-clock VFR PTS the
                    # gap is preserved as a held frame, so timing stays honest.
                    # The seq only advances on a successful enqueue, so the
                    # sequence the encode thread consumes has no gaps.
                    self.frame_q.put_nowait((self._capture_seq, screenshot, time.time(), info))
                    self._capture_seq += 1
                    self.recording_pts += 1
                except queue.Full:
                    self.dropped_frames += 1

            print('stopping bc')
            bc.stop()
            del bc

    def _preprocess_worker(self):
        # One of N parallel workers. Does all the CPU-heavy per-frame work and
        # hands an encode-ready yuv420p frame to the ordered encode thread, keyed
        # by capture sequence number.
        while True:
            item = self.frame_q.get()
            if item is None:  # shutdown sentinel from stop()
                return
            seq, screenshot, unix_stamp, info = item
            result = None
            try:
                # Apply DPI-aware cursor compositing *before* resizing.
                if info:
                    composite_cursor_dpi_aware(screenshot, info)

                current_height, current_width, _ = screenshot.shape
                target_width, target_height = current_width, current_height
                if self.target_res != 'Screen':
                    target_width, target_height = calculate_scaled_resolution(
                        current_width, current_height, target_height=int(self.target_res))

                # Resize + colour-convert with OpenCV rather than swscale: cv2
                # releases the GIL (so the pool actually scales across cores) and
                # is faster here, while INTER_LANCZOS4 keeps the high-quality
                # downscale. The encode thread then only feeds the GPU encoder.
                bgr = screenshot
                if (target_width, target_height) != (current_width, current_height):
                    bgr = cv2.resize(bgr, (target_width, target_height), interpolation=cv2.INTER_LANCZOS4)
                i420 = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)  # planar yuv420p layout
                frame = av.VideoFrame.from_ndarray(i420, format='yuv420p')
                result = (frame, unix_stamp)
            except Exception as e:
                print(f'preprocess error (seq {seq}): {e}')
                result = None

            with self._results_cond:
                self._results[seq] = result
                self._results_cond.notify_all()

    def _encode_worker(self):
        # Single ordered consumer: pulls preprocessed frames strictly in capture
        # order, runs the stateful encoder, and appends to the time-ordered ring
        # buffer. Keeping this serial is required for both the encoder state and
        # the buffer ordering that clip extraction relies on.
        self.encoding_pts = 0
        codec_dims_set = False
        while True:
            with self._results_cond:
                while self._next_encode_seq not in self._results:
                    if self._final_seq is not None and self._next_encode_seq >= self._final_seq:
                        return  # drained on stop()
                    self._results_cond.wait(timeout=0.5)
                result = self._results.pop(self._next_encode_seq)
                self._next_encode_seq += 1

            if result is None:
                continue  # frame failed in preprocessing; skip it without a gap

            frame, unix_stamp = result

            # The frame is already at the target size; configure the codec once
            # before the first encode of this session.
            if not codec_dims_set:
                self.codec.width = frame.width
                self.codec.height = frame.height
                self.width, self.height = frame.width, frame.height
                codec_dims_set = True

            frame.pts = self.frame_idx
            packets = self.codec.encode(frame)  # list of Packet objects
            self.frame_idx += 1

            keyframe = any(pkt.is_keyframe for pkt in packets)

            self.frames_buffer.append((packets, unix_stamp, self.codec, keyframe))

            # Maintain a rolling buffer of frames
            if self.frames_buffer:
                while time.time() - self.frames_buffer[0][1] > self.clip_duration:
                    self.frames_buffer.pop(0)

            # Wake any clip() waiting for the buffer to reach its requested time.
            with self._buffer_cond:
                self._buffer_cond.notify_all()

            self.encoding_pts += 1

    def reset_buffer(self):
        self.frames_buffer = []

    # ------------------------------------------------------------------
    # GPU pipeline (primary): ffmpeg  ddagrab -> scale_d3d11 -> h264_nvenc
    # -> mpegts pipe -> PyAV demux -> ring buffer. Capture, downscale and
    # encode all happen on the GPU, so this costs ~0% CPU.
    # ------------------------------------------------------------------
    _gpu_capable = None  # class-level cache: None=unknown, then True/False

    @classmethod
    def _probe_gpu(cls):
        """Check once whether the GPU capture pipeline works on this machine
        (Desktop Duplication + D3D11 scaler + NVENC, all via ffmpeg)."""
        if cls._gpu_capable is not None:
            return cls._gpu_capable
        ok = False
        try:
            cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error',
                   '-f', 'lavfi', '-i', 'ddagrab=framerate=30',
                   '-vf', 'scale_d3d11=640:360:format=nv12',
                   '-c:v', 'h264_nvenc', '-frames:v', '3', '-f', 'null', '-']
            r = subprocess.run(cmd, capture_output=True, timeout=20)
            ok = (r.returncode == 0)
        except Exception as e:
            print(f'GPU capture probe failed: {e}')
        cls._gpu_capable = ok
        print(f'GPU capture pipeline {"available" if ok else "unavailable"} '
              f'-> using {"GPU" if ok else "CPU"} path.')
        return ok

    def _build_ffmpeg_cmd(self):
        fps = max(1, int(round(self.fps)))
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error',
               '-f', 'lavfi',
               '-i', (f'ddagrab=framerate={fps}'
                      f':draw_mouse={1 if self.record_mouse else 0}'
                      f':output_idx={self.monitor_index}')]
        if self.target_res != 'Screen':
            nw, nh = _primary_resolution()
            tw, th = calculate_scaled_resolution(nw, nh, target_height=int(self.target_res))
            tw -= tw % 2  # NVENC wants even dimensions
            th -= th % 2
            cmd += ['-vf', f'scale_d3d11={tw}:{th}:format=nv12']
        cmd += ['-c:v', 'h264_nvenc', '-preset', 'p4', '-rc', 'vbr', '-cq', '18',
                '-b:v', str(self.bitrate), '-maxrate', str(self.bitrate * 2),
                '-bufsize', str(self.bitrate * 4), '-g', str(fps), '-bf', '0',
                # Force constant frame rate at the target: ddagrab/DDA only delivers
                # frames as the desktop composites them (well below the monitor
                # refresh for windowed content), so duplicate to hit <fps>. A real
                # fullscreen game presenting at <fps> yields all-unique frames.
                '-r', str(fps),
                # Low-latency muxing so frames don't sit in the mpegts buffer.
                '-muxdelay', '0', '-muxpreload', '0', '-flush_packets', '1',
                '-f', 'mpegts', 'pipe:1']
        return cmd

    def _gpu_reader(self):
        """Demux the ffmpeg mpegts pipe into the rolling buffer. Each entry is
        ([packet], wall_stamp, codec_descriptor, keyframe) -- the SAME shape the
        CPU encode worker produces, so encoder.encode_streams is unchanged."""
        proc = self._ffmpeg_proc
        try:
            # Small probe so PyAV starts delivering packets quickly (keeps the
            # capture->buffer latency low and stable).
            container = av.open(_PipeReader(proc.stdout), format='mpegts',
                                options={'probesize': '32', 'analyzeduration': '0',
                                         'fflags': 'nobuffer'})
        except Exception as e:
            print(f'GPU reader: could not open ffmpeg stream: {e}')
            return
        vstream = container.streams.video[0]
        cc = vstream.codec_context

        # Stream-copy params for the clip muxer. Plain codec name 'h264' (not an
        # encoder) so add_stream() makes a copy target without opening a second
        # NVENC session. GPU clips are keyframe-snap (no head re-encode), so the
        # encoder options aren't needed here.
        try:
            pix_fmt = cc.format.name
        except Exception:
            pix_fmt = 'yuv420p'
        desc = _StreamParams('h264', cc.width or self.width, cc.height or self.height,
                             pix_fmt, Fraction(1, max(1, int(round(self.fps)))))
        self._gpu_codec = desc
        self.width, self.height = desc.width, desc.height

        tb = vstream.time_base or Fraction(1, 90000)
        t0_wall = None
        pts0 = None
        try:
            for packet in container.demux(vstream):
                if not self._is_recording:
                    break
                if packet.pts is None or packet.dts is None or packet.size == 0:
                    continue
                # Stamp from ffmpeg's frame PTS (accurately paced by ddagrab),
                # NOT the pipe arrival time -- PyAV delivers packets in bursts, so
                # arrival times are clumped and would collapse the clip's PTS.
                # Anchor to the first frame's wall clock so the stamp still tracks
                # real time for buffer eviction.
                if t0_wall is None:
                    t0_wall = time.time()
                    pts0 = packet.pts
                # Subtract the pipeline latency so video stamps line up with the
                # real-time-stamped audio (otherwise audio leads the video).
                stamp = t0_wall - self.gpu_av_latency + float((packet.pts - pts0) * tb)
                kf = bool(packet.is_keyframe)
                clone = av.Packet(bytes(packet))
                clone.is_keyframe = kf
                clone.pts = packet.pts
                clone.dts = packet.dts
                clone.time_base = packet.time_base
                self.frames_buffer.append(([clone], stamp, desc, kf))
                if self.frames_buffer:
                    while time.time() - self.frames_buffer[0][1] > self.clip_duration:
                        self.frames_buffer.pop(0)
                with self._buffer_cond:
                    self._buffer_cond.notify_all()
        except Exception as e:
            if self._is_recording:
                print(f'GPU reader stopped: {e}')
        finally:
            try:
                container.close()
            except Exception:
                pass

    def _start_gpu(self):
        self._mode = 'gpu'
        self.supports_precise_clip = False  # keyframe-snap clips
        self.frames_buffer = []
        self.width = self.height = None
        self._gpu_codec = None
        self._is_recording = True
        cmd = self._build_ffmpeg_cmd()
        self._ffmpeg_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10 ** 8)
        self._gpu_thread = threading.Thread(target=self._gpu_reader, daemon=True)
        self._gpu_thread.start()
        print(f"Screen recording started (GPU pipeline, res={self.target_res}, "
              f"{int(round(self.fps))}fps).")

    def _stop_gpu(self):
        self._is_recording = False
        proc = self._ffmpeg_proc
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except Exception:
                    proc.kill()
            except Exception:
                pass
        if self._gpu_thread is not None:
            self._gpu_thread.join(timeout=2)
        self._gpu_thread = None
        self._ffmpeg_proc = None
        self._gpu_codec = None
        self.reset_buffer()
        print("Screen recording stopped (GPU).")

    # ------------------------------------------------------------------
    # Public start/stop: route to the GPU pipeline when available, else the
    # bettercam + cv2 worker-pool fallback.
    # ------------------------------------------------------------------
    def start(self):
        if self._is_recording:
            print("Already recording.")
            return
        if self._probe_gpu():
            self._start_gpu()
        else:
            self._start_cpu()

    def stop(self):
        if not self._is_recording:
            print("Not currently recording.")
            return
        if self._mode == 'gpu':
            self._stop_gpu()
        else:
            self._stop_cpu()
        self._mode = None

    def _start_cpu(self):
        self._mode = 'cpu'
        self.supports_precise_clip = True  # head re-encode gives exact clip start
        while not self.frame_q.empty():
            try:
                self.frame_q.get_nowait()
            except queue.Empty:
                break

        gpu_enc = 'h264_nvenc'
        cpu_enc = 'libx264'
        use_enc = None

        self.height, self.width, self.codec = None, None, None

        self.frame_idx = 0

        try:
            self.codec = av.codec.CodecContext.create(gpu_enc, 'w')
            use_enc = gpu_enc
            print('Encoding using GPU')
        except:
            self.codec = av.codec.CodecContext.create(cpu_enc, 'w')
            use_enc = cpu_enc
            print('Encoding not able to use GPU, falling back to CPU')

        self.codec.pix_fmt = 'yuv420p'
        self.codec.time_base = Fraction(1, self.fps)
        # ~1 second keyframe interval. NVENC's default GOP is huge, which left
        # only a keyframe or two in the whole replay buffer, so a clip could only
        # start at one of those sparse points (giving the wrong duration). With
        # ~1s keyframes the clip starts within a second of the requested point,
        # and the encoder's head re-encode nails the exact start.
        self.codec.gop_size = max(1, int(round(self.fps)))

        if 'nvenc' in use_enc:
            # NVENC options for speed
            self.codec.options = {
                'preset': 'fast',
                'rc': 'vbr',
                'cq': '18',
                'b:v': f'{self.bitrate}',  # 5 Mbps bitrate
                'maxrate': f'{self.bitrate * 2}',
                'bufsize': f'{self.bitrate * 4}',
                'bf': '0'  # no B-frames: keeps decode order == display order and
                           # pts == dts, so the encoder's wall-clock PTS rebasing is exact
            }
        else:
            # CPU encoder options for speed
            self.codec.options = {
                'preset': 'veryfast',  # Much faster than 'fast'
                'crf': '23',
                'tune': 'zerolatency',  # Optimize for speed
                'threads': '0'  # Use all available threads
            }

        # Reset the pipeline handoff state for this session.
        self._results = {}
        self._results_cond = threading.Condition()
        self._capture_seq = 0
        self._next_encode_seq = 0
        self._final_seq = None

        self._is_recording = True
        self.frames_buffer = []

        # Spawn the ordered encode thread + parallel preprocess pool, then the
        # capture thread.
        self._encode_thread = threading.Thread(target=self._encode_worker, daemon=True)
        self._encode_thread.start()
        self._preprocess_threads = [
            threading.Thread(target=self._preprocess_worker, daemon=True)
            for _ in range(self.num_preprocess_workers)
        ]
        for t in self._preprocess_threads:
            t.start()

        self._recording_thread = threading.Thread(target=self._record_loop, daemon=True)
        self._recording_thread.start()
        print(f"Screen recording started ({self.num_preprocess_workers} preprocess workers).")

    def _stop_cpu(self):
        #print("Stopping recording...")
        self._is_recording = False
        if self._recording_thread and self._recording_thread.is_alive():
            self._recording_thread.join()

        # Capture has stopped; drain the preprocess pool with one sentinel each,
        # then let the encode thread finish everything already captured.
        for _ in self._preprocess_threads:
            self.frame_q.put(None)
        for t in self._preprocess_threads:
            t.join()
        self._preprocess_threads = []

        with self._results_cond:
            self._final_seq = self._capture_seq  # every captured frame now has a result
            self._results_cond.notify_all()
        if self._encode_thread:
            self._encode_thread.join()
            self._encode_thread = None

        self.reset_buffer()

        print("Screen recording stopped.")

    def clip(self, args=None):
        clip_time = time.time()
        deadline = clip_time + 5.0  # safety timeout so a stalled pipeline can't hang clip()

        with self._buffer_cond:
            while True:
                buf = self.frames_buffer
                if buf and buf[-1][1] >= clip_time:
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._buffer_cond.wait(timeout=remaining)
            # Return a snapshot of the references so the buffer can keep mutating
            # underneath us while the clip is being assembled.
            return list(self.frames_buffer)

    def write_file(self, frames, filename):
        if not frames:
            return

        # frames is a 2d array of (frame data, timestamp)
        height, width, _ = frames[0][0].shape
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        fps = len(frames)/(frames[-1][1]-frames[0][1])
        out = cv2.VideoWriter(filename, fourcc, fps, (width, height))

        if not out.isOpened():
            print(f"Error: Could not open video writer for {filename}")
            return

        for frame_bgr in frames:
            out.write(frame_bgr[0])
        out.release()
        print(f"Video saved: {filename} ({len(frames)} frames).")