from threading import Condition, Thread
import time


class LatestFrameCapture:
    def __init__(self, capture, initial_frame=None, read_timeout_seconds: float = 0.25):
        self.capture = capture
        self._condition = Condition()
        self._latest_ok = initial_frame is not None
        self._latest_frame = initial_frame
        self._version = 1 if initial_frame is not None else 0
        self._last_returned_version = 0
        self._running = True
        self._read_timeout_seconds = read_timeout_seconds
        self._thread = Thread(target=self._read_frames, daemon=True)
        self._thread.start()

    def __getattr__(self, name):
        return getattr(self.capture, name)

    def _read_frames(self):
        while self._running:
            try:
                ok, frame = self.capture.read()
            except Exception:
                ok, frame = False, None
            with self._condition:
                if not self._running:
                    break
                self._latest_ok = ok
                self._latest_frame = frame
                self._version += 1
                self._condition.notify_all()
            if not ok:
                time.sleep(0.02)

    def read(self):
        with self._condition:
            if self._version == self._last_returned_version and self._running:
                self._condition.wait(timeout=self._read_timeout_seconds)
            if self._version == self._last_returned_version:
                return False, None
            self._last_returned_version = self._version
            return self._latest_ok, self._latest_frame

    def release(self):
        with self._condition:
            self._running = False
            self._condition.notify_all()
        try:
            self.capture.release()
        except Exception:
            pass
        self._thread.join(timeout=1)
        if self._thread.is_alive():
            raise RuntimeError("Camera reader thread did not stop after release.")


class VideoFileCapture:
    def __init__(self, capture, cv2, fps: float, loop: bool = False, realtime: bool = True, initial_frame=None):
        self.capture = capture
        self.cv2 = cv2
        self.loop = loop
        self._initial_frame = initial_frame
        self._frame_interval_seconds = 1 / fps if realtime and fps and fps > 0 else 0
        self._next_frame_at = None

    def __getattr__(self, name):
        return getattr(self.capture, name)

    def read(self):
        if self._initial_frame is not None:
            frame = self._initial_frame
            self._initial_frame = None
            self._schedule_next_frame()
            return True, frame

        self._pace()
        ok, frame = self.capture.read()
        if not ok and self.loop:
            self.capture.set(self.cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.capture.read()
        if ok:
            self._schedule_next_frame()
        return ok, frame

    def release(self):
        try:
            self.capture.release()
        except Exception:
            pass

    def _pace(self):
        if self._next_frame_at is None:
            return
        remaining = self._next_frame_at - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    def _schedule_next_frame(self):
        if self._frame_interval_seconds <= 0:
            return
        now = time.monotonic()
        if self._next_frame_at is None or self._next_frame_at < now - self._frame_interval_seconds:
            self._next_frame_at = now + self._frame_interval_seconds
        else:
            self._next_frame_at += self._frame_interval_seconds


def _set_capture_property(capture, cv2, property_name: str, value: int) -> None:
    property_id = getattr(cv2, property_name, None)
    if property_id is not None:
        capture.set(property_id, value)


def open_camera_capture(
    cv2,
    preferred_index: int,
    fallback_index: int,
    width: int,
    height: int,
    low_latency: bool = False,
    auto_scan: bool = True,
):
    candidates = []
    for idx in (preferred_index, fallback_index):
        if idx not in candidates:
            candidates.append(idx)
    if auto_scan:
        for idx in (0, 1, 2, 3):
            if idx not in candidates:
                candidates.append(idx)

    attempted_indexes = []
    for index in candidates:
        if index in attempted_indexes:
            continue
        attempted_indexes.append(index)

        captures_to_try = [lambda i=index: cv2.VideoCapture(i)]
        if hasattr(cv2, "CAP_DSHOW"):
            captures_to_try.append(lambda i=index: cv2.VideoCapture(i, cv2.CAP_DSHOW))

        for open_fn in captures_to_try:
            try:
                capture = open_fn()
            except Exception:
                continue

            if capture.isOpened():
                if low_latency:
                    _set_capture_property(capture, cv2, "CAP_PROP_BUFFERSIZE", 1)
                capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                try:
                    ok, frame = capture.read()
                except Exception:
                    ok, frame = False, None
                if ok and frame is not None:
                    if low_latency:
                        return LatestFrameCapture(capture, initial_frame=frame), index
                    return capture, index
            try:
                capture.release()
            except Exception:
                pass

    attempted = ", ".join(str(index) for index in attempted_indexes)
    raise RuntimeError(f"Unable to open camera index {attempted}")


def open_video_capture(cv2, source: str, loop: bool = False, realtime: bool = True):
    capture = cv2.VideoCapture(source)
    if not capture.isOpened():
        capture.release()
        raise RuntimeError(f"Unable to open video source {source}")

    ok, initial_frame = capture.read()
    if not ok or initial_frame is None:
        capture.release()
        raise RuntimeError(f"Unable to read first frame from video source {source}")

    fps = capture.get(cv2.CAP_PROP_FPS) or 0
    return VideoFileCapture(capture, cv2, fps=fps, loop=loop, realtime=realtime, initial_frame=initial_frame)
