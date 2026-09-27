from dataclasses import dataclass

from .errors import WireError


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """A stream format, as in `start{format}`: one `frame_ms`."""

    codec: str
    rate: int
    channels: int
    frame_ms: int

    @property
    def frame_samples(self) -> int:
        return self.rate * self.frame_ms // 1000

    @property
    def frame_bytes(self) -> int:
        return self.frame_samples * self.channels * 2

    def check_frame(self, payload: bytes, *, final: bool) -> None:
        """Enforce the frame rules of docs/protocol/audio.md. Raises `WireError`.

        Only the stream's final frame may be short, and it still holds at least one sample.
        """
        if self.codec != "pcm_s16le":
            raise ValueError(f"no frame rules for codec {self.codec!r}")
        size = len(payload)
        if size % (self.channels * 2):
            raise WireError("partial_sample", f"{size} bytes isn't a whole number of samples")
        if size > self.frame_bytes:
            raise WireError(
                "oversized_frame", f"{size} bytes, the frame size is {self.frame_bytes}"
            )
        if size == 0 or (size < self.frame_bytes and not final):
            raise WireError("short_frame", f"{size} bytes, the frame size is {self.frame_bytes}")
