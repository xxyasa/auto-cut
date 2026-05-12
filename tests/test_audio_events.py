import math
import unittest
from pathlib import Path
from unittest.mock import patch

from autocut.audio_events import detect_cough_like_events
from autocut.models import CandidateClip, TranscriptSegment


class AudioEventsTest(unittest.TestCase):
    def test_detect_cough_like_event_in_asr_gap(self):
        audio_path = Path("sample.wav")
        sample_rate, samples = _test_samples()
        with (
            patch("autocut.audio_events.Path.exists", return_value=True),
            patch("autocut.audio_events._read_wav_mono", return_value=(sample_rate, samples)),
        ):
            candidate = CandidateClip(
                clip_id="clip_audio",
                source_video=Path("sample.mp4"),
                start_time=0,
                end_time=3,
                transcript="前面 后面",
                clean_transcript="前面 后面",
                segment_indexes=[0],
            )
            segment = TranscriptSegment(
                start=0,
                end=3,
                text="前面 后面",
                words=[
                    {"start": 0.2, "end": 0.5, "word": "前面"},
                    {"start": 2.1, "end": 2.4, "word": "后面"},
                ],
            )

            events = detect_cough_like_events(audio_path, candidate, [segment])

        self.assertTrue(events)
        self.assertEqual(events[0]["kind"], "audio_cough_like")
        self.assertGreaterEqual(events[0]["start"], 1.0)
        self.assertLessEqual(events[0]["end"], 1.6)


def _test_samples() -> tuple[int, list[float]]:
    sample_rate = 16000
    samples: list[float] = []
    total = sample_rate * 3
    for index in range(total):
        time = index / sample_rate
        value = 0.002 * math.sin(2 * math.pi * 180 * time)
        if 1.15 <= time <= 1.38:
            value += 0.42 * math.sin(2 * math.pi * 1800 * time)
            value += 0.28 * math.sin(2 * math.pi * 3100 * time)
            value += 0.20 * math.sin(2 * math.pi * 5100 * time)
        samples.append(max(-1.0, min(1.0, value)))
    return sample_rate, samples


if __name__ == "__main__":
    unittest.main()
