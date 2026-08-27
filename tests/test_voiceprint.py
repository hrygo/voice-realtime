"""CAM++ 声纹嵌入、在线质心池与全局 AHC 聚类测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from voice_realtime.meeting.models import NormalizedSegment
from voice_realtime.meeting.voiceprint import (
    AHCClusterer,
    AudioMemoryBuffer,
    CAMPlusExtractor,
    CentroidPool,
    MeetingVoiceprintManager,
    VoiceprintProfileMatcher,
    _cosine_similarity,
)


def test_cosine_similarity_basic() -> None:
    v1 = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    v2 = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    v3 = np.array([0.0, 1.0, 0.0], dtype=np.float32)
    v4 = np.array([-1.0, 0.0, 0.0], dtype=np.float32)

    assert pytest.approx(_cosine_similarity(v1, v2)) == 1.0
    assert pytest.approx(_cosine_similarity(v1, v3)) == 0.0
    assert pytest.approx(_cosine_similarity(v1, v4)) == -1.0


def test_audio_memory_buffer_slice_and_clear() -> None:
    buffer = AudioMemoryBuffer(sample_rate=16000)
    # 1 second of 16kHz 16-bit mono PCM = 32000 bytes (16000 int16 samples)
    one_sec = np.ones(16000, dtype=np.int16).tobytes()
    buffer.append(one_sec)
    buffer.append(one_sec)

    assert buffer.duration_secs == pytest.approx(2.0)
    # Slice 500ms to 1500ms -> 1000ms duration = 32000 bytes
    slice_data = buffer.get_slice(500, 1500)
    assert len(slice_data) == 32000

    # Invalid slices
    assert buffer.get_slice(1000, 500) == b""
    assert buffer.get_slice(-100, 500) == b""
    assert buffer.get_slice(3000, 4000) == b""

    buffer.clear()
    assert buffer.duration_secs == 0.0
    assert len(buffer.get_slice(0, 1000)) == 0


def test_camplus_extractor_ignores_short_audio() -> None:
    extractor = CAMPlusExtractor(model_path=Path("/tmp/nonexistent.onnx"), min_duration_secs=0.5)
    # 0.2s of audio (3200 samples)
    short_audio = np.zeros(3200, dtype=np.int16).tobytes()
    assert extractor.extract_embedding(short_audio) is None


def test_camplus_extractor_with_real_onnx_model() -> None:
    from voice_realtime.model_cache import huggingface_snapshot_path

    model_path = (
        huggingface_snapshot_path(
            "csukuangfj/speaker-embedding-models",
            revision="0743f301363dec56491a490f6d6cbc9d67f9a3bf",
        )
        / "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
    )
    if not model_path.exists():
        pytest.skip("CAM++ 模型未下载")

    extractor = CAMPlusExtractor(model_path=model_path, min_duration_secs=0.5)
    # 1.5 seconds of 16kHz random audio
    audio_data = np.random.randn(24000).astype(np.float32)
    emb = extractor.extract_embedding(audio_data)

    assert emb is not None
    assert emb.shape == (192,)
    assert pytest.approx(float(np.linalg.norm(emb)), abs=1e-4) == 1.0


def test_centroid_pool_merges_similar_speaker_tracks() -> None:
    pool = CentroidPool(merge_threshold=0.75)

    base_vector = np.zeros(192, dtype=np.float32)
    base_vector[0] = 1.0  # speaker A canonical direction

    spk_a_emb1 = base_vector.copy()
    resolved_a1 = pool.add_embedding("epoch0:s0", spk_a_emb1)
    assert resolved_a1 == "epoch0:s0"

    # Slightly perturbed vector (cosine similarity ~0.99 > 0.75)
    spk_b_emb = base_vector.copy()
    spk_b_emb[1] = 0.05
    spk_b_emb = spk_b_emb / np.linalg.norm(spk_b_emb)

    # Sortformer assigned a new channel epoch1:s1 due to reconnection/pause
    resolved_b = pool.add_embedding("epoch1:s1", spk_b_emb)
    assert resolved_b == "epoch0:s0"
    assert pool.get_canonical("epoch1:s1") == "epoch0:s0"


def test_centroid_pool_keeps_distinct_speakers_separated() -> None:
    pool = CentroidPool(merge_threshold=0.75)

    spk_a_emb = np.zeros(192, dtype=np.float32)
    spk_a_emb[0] = 1.0

    spk_b_emb = np.zeros(192, dtype=np.float32)
    spk_b_emb[1] = 1.0  # orthogonal direction (cosine similarity 0.0)

    res_a = pool.add_embedding("epoch0:s0", spk_a_emb)
    res_b = pool.add_embedding("epoch0:s1", spk_b_emb)

    assert res_a == "epoch0:s0"
    assert res_b == "epoch0:s1"
    assert pool.get_canonical("epoch0:s0") == "epoch0:s0"
    assert pool.get_canonical("epoch0:s1") == "epoch0:s1"
    assert pool.speaker_count == 2


def test_ahc_clusterer_merges_over_split_speakers() -> None:
    clusterer = AHCClusterer(distance_threshold=0.35, max_speakers=4)

    # Simulate speaker 1 embeddings across 3 fragmented tracks (e.g. s0, s2, s3)
    v1 = np.zeros(192, dtype=np.float32)
    v1[0] = 1.0

    # Simulate speaker 2 embeddings (s1)
    v2 = np.zeros(192, dtype=np.float32)
    v2[1] = 1.0

    embeddings: list[tuple[str, np.ndarray]] = [
        ("epoch0:s0", v1.copy()),
        ("epoch0:s0", v1.copy()),
        ("epoch0:s1", v2.copy()),
        ("epoch0:s1", v2.copy()),
        ("epoch1:s2", v1 + np.random.normal(0, 0.01, 192)),  # same as v1
        ("epoch2:s3", v1 + np.random.normal(0, 0.01, 192)),  # same as v1
    ]

    remapping = clusterer.cluster_speakers(embeddings)
    # epoch1:s2 and epoch2:s3 should be mapped to epoch0:s0 (the dominant track)
    assert remapping.get("epoch1:s2") == "epoch0:s0"
    assert remapping.get("epoch2:s3") == "epoch0:s0"
    # s1 should NOT be remapped to s0
    assert "epoch0:s1" not in remapping


def test_ahc_clusterer_respects_max_speakers_constraint() -> None:
    # 5 slightly separated speakers, but max_speakers is constrained to 2
    clusterer = AHCClusterer(distance_threshold=0.10, max_speakers=2)

    # 3 speakers close to group A, 2 speakers close to group B
    v_a = np.zeros(192, dtype=np.float32)
    v_a[0] = 1.0
    v_b = np.zeros(192, dtype=np.float32)
    v_b[1] = 1.0

    embeddings = [
        ("s0", v_a.copy()),
        ("s1", v_a + np.array([0.0, 0.0, 0.05] + [0.0] * 189)),
        ("s2", v_a + np.array([0.0, 0.0, 0.08] + [0.0] * 189)),
        ("s3", v_b.copy()),
        ("s4", v_b + np.array([0.0, 0.0, 0.05] + [0.0] * 189)),
    ]

    remapping = clusterer.cluster_speakers(embeddings)
    # The final mapping must reduce the unique speaker tracks to at most 2
    all_keys = {"s0", "s1", "s2", "s3", "s4"}
    final_speakers = {remapping.get(k, k) for k in all_keys}
    assert len(final_speakers) <= 2


def test_meeting_voiceprint_manager_lifecycle() -> None:
    fake_extractor = MagicMock(spec=CAMPlusExtractor)
    dummy_emb = np.zeros(192, dtype=np.float32)
    dummy_emb[0] = 1.0
    fake_extractor.extract_embedding.return_value = dummy_emb

    manager = MeetingVoiceprintManager(extractor=fake_extractor, enabled=True)

    # Append 3 seconds of dummy PCM audio (16kHz 16bit = 96000 bytes)
    dummy_pcm = np.ones(48000, dtype=np.int16).tobytes()
    manager.append_audio(dummy_pcm)

    from uuid import uuid4

    seg1 = NormalizedSegment(
        id=uuid4(),
        order=0,
        source_epoch=0,
        speaker_key="epoch0:s0",
        start_ms=0,
        end_ms=1000,
        text="第一句话",
    )
    seg2 = NormalizedSegment(
        id=uuid4(),
        order=1,
        source_epoch=1,
        speaker_key="epoch1:s1",
        start_ms=1200,
        end_ms=2500,
        text="第二句话",
    )

    manager.process_segments([seg1, seg2])
    assert len(manager._segment_embeddings) == 2
    assert fake_extractor.extract_embedding.call_count == 2

    # Global remapping
    remapping = manager.compute_global_remapping(max_speakers=1)
    assert remapping == {"epoch1:s1": "epoch0:s0"}

    # Clear
    manager.clear()
    assert len(manager._segment_embeddings) == 0
    assert manager.audio_buffer.duration_secs == 0.0


def test_voiceprint_profile_matcher() -> None:
    matcher = VoiceprintProfileMatcher(match_threshold=0.72)

    # Enroll user Alice (along dimension 0) and Bob (along dimension 1)
    alice_emb = np.zeros(192, dtype=np.float32)
    alice_emb[0] = 1.0
    matcher.enroll("user_001", "Alice", alice_emb)

    bob_emb = np.zeros(192, dtype=np.float32)
    bob_emb[1] = 1.0
    matcher.enroll("user_002", "Bob", bob_emb)

    # Test match with Alice
    test_emb = alice_emb.copy()
    test_emb[2] = 0.05
    res = matcher.match_centroid(test_emb)
    assert res is not None
    user_id, display_name, sim = res
    assert user_id == "user_001"
    assert display_name == "Alice"
    assert sim > 0.9

    # Test unknown speaker (orthogonal dimension 2)
    unknown_emb = np.zeros(192, dtype=np.float32)
    unknown_emb[2] = 1.0
    assert matcher.match_centroid(unknown_emb) is None


def test_meeting_voiceprint_manager_match_enrolled_speakers() -> None:
    fake_extractor = MagicMock(spec=CAMPlusExtractor)
    alice_emb = np.zeros(192, dtype=np.float32)
    alice_emb[0] = 1.0
    fake_extractor.extract_embedding.return_value = alice_emb

    manager = MeetingVoiceprintManager(extractor=fake_extractor, enabled=True)
    matcher = VoiceprintProfileMatcher(match_threshold=0.72)
    matcher.enroll("user_001", "Alice", alice_emb)

    # Add audio and process a segment for epoch0:s0
    manager.append_audio(np.ones(32000, dtype=np.int16).tobytes())
    from uuid import uuid4

    seg = NormalizedSegment(
        id=uuid4(),
        order=0,
        source_epoch=0,
        speaker_key="epoch0:s0",
        start_ms=0,
        end_ms=1000,
        text="Hello world",
    )
    manager.process_segments([seg])

    matched = manager.match_enrolled_speakers(matcher)
    assert matched == {"epoch0:s0": "Alice"}

