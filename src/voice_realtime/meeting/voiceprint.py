"""CAM++ 声纹嵌入提取、在线质心池跟踪与全局 AHC 聚类二次修正。

架构层级：
1. CAMPlusExtractor: 基于 3D-Speaker CAM++ ONNX (~27MB) 的 192 维 L2 归一化声纹特征提取器；
2. CentroidPool: 会议实时在线质心池，动态合并高相似度声道，防一人多号；
3. AHCClusterer: 会议结束时基于层次凝聚聚类 (AHC) 与 max_speakers 约束的全局声纹二次修正。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import numpy as np
import torch
import torchaudio.compliance.kaldi as kaldi  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


def _cosine_similarity(v1: np.ndarray, v2: np.ndarray) -> float:
    """计算两个单位向量的余弦相似度。"""
    dot = float(np.dot(v1, v2))
    return max(-1.0, min(1.0, dot))


class CAMPlusExtractor:
    """CAM++ 192 维声纹特征提取器 (ONNX 运行时)。"""

    def __init__(
        self,
        model_path: Path | str | None = None,
        *,
        min_duration_secs: float = 0.5,
    ) -> None:
        self.model_path = Path(model_path) if model_path is not None else None
        self.min_duration_secs = min_duration_secs
        self._session: Any | None = None

    def _get_session(self) -> Any:
        if self._session is not None:
            return self._session

        if self.model_path is None or not self.model_path.exists():
            raise FileNotFoundError(
                f"CAM++ 声纹模型不存在: {self.model_path}；"
                "请运行 scripts/download-models.sh 准备模型"
            )

        import onnxruntime as ort  # type: ignore[import-untyped]

        sess_options = ort.SessionOptions()
        sess_options.intra_op_num_threads = 2
        sess_options.inter_op_num_threads = 1
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self._session = ort.InferenceSession(
            str(self.model_path),
            sess_options=sess_options,
            providers=["CPUExecutionProvider"],
        )
        return self._session

    def extract_fbank(self, waveform: torch.Tensor) -> np.ndarray:
        """从 16kHz 单声道音频提取 80 维 log-mel filterbank 特征。"""
        if waveform.ndim == 1:
            waveform = waveform.unsqueeze(0)
        elif waveform.ndim == 2 and waveform.shape[0] > 1:
            waveform = waveform[:1, :]

        feat = kaldi.fbank(
            waveform,
            num_mel_bins=80,
            sample_frequency=16000,
            dither=0.0,
            energy_floor=0.0,
        )
        # CMVN 归一化
        feat = feat - feat.mean(dim=0, keepdim=True)
        return cast(np.ndarray, feat.unsqueeze(0).cpu().numpy().astype(np.float32))

    def extract_embedding(
        self, audio: np.ndarray | torch.Tensor | bytes
    ) -> np.ndarray | None:
        """提取 192 维 L2 归一化声纹向量；若音频时长不足 min_duration_secs 则返回 None。"""
        if isinstance(audio, bytes):
            # 16-bit PCM 16kHz mono bytes
            samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
            tensor_audio = torch.from_numpy(samples)
        elif isinstance(audio, np.ndarray):
            if audio.dtype == np.int16:
                samples = audio.astype(np.float32) / 32768.0
            else:
                samples = audio.astype(np.float32)
            tensor_audio = torch.from_numpy(samples)
        elif isinstance(audio, torch.Tensor):
            tensor_audio = audio.float()
            if tensor_audio.dtype == torch.int16:
                tensor_audio = tensor_audio.float() / 32768.0
        else:
            raise TypeError(f"不支持的音频输入类型: {type(audio)}")

        total_samples = tensor_audio.numel()
        min_samples = int(16000 * self.min_duration_secs)
        if total_samples < min_samples:
            return None

        try:
            session = self._get_session()
            feat_np = self.extract_fbank(tensor_audio)
            input_name = session.get_inputs()[0].name
            outputs = session.run(None, {input_name: feat_np})
            raw_emb = outputs[0][0]  # shape (192,)
            norm = float(np.linalg.norm(raw_emb))
            if norm < 1e-6:
                return None
            return cast(np.ndarray, (raw_emb / norm).astype(np.float32))
        except Exception as exc:
            logger.warning("CAMPlusExtractor: 提取声纹特征失败: %s", exc)
            return None


class CentroidPool:
    """实时在线说话人声纹质心池。"""

    def __init__(self, merge_threshold: float = 0.75) -> None:
        self.merge_threshold = merge_threshold
        self._centroids: dict[str, np.ndarray] = {}
        self._counts: dict[str, int] = defaultdict(int)
        self._aliases: dict[str, str] = {}

    def get_canonical(self, speaker_key: str) -> str:
        """获取规范化的 speaker_key（跟随别名链）。"""
        curr = speaker_key
        visited: set[str] = set()
        while curr in self._aliases and curr not in visited:
            visited.add(curr)
            curr = self._aliases[curr]
        return curr

    def get_centroid(self, speaker_key: str) -> np.ndarray | None:
        """获取指定说话人的当前质心。"""
        canonical = self.get_canonical(speaker_key)
        return self._centroids.get(canonical)

    def add_embedding(self, speaker_key: str, embedding: np.ndarray) -> str:
        """将声纹嵌入并入质心池，返回解析后的规范 speaker_key。"""
        norm = np.linalg.norm(embedding)
        if norm < 1e-6:
            return self.get_canonical(speaker_key)
        unit_emb = (embedding / norm).astype(np.float32)

        canonical = self.get_canonical(speaker_key)

        # 检查是否与已有的其他说话人质心高度相似
        best_match: str | None = None
        best_sim = -1.0
        for spk, centroid in self._centroids.items():
            if spk == canonical:
                continue
            sim = _cosine_similarity(unit_emb, centroid)
            if sim > best_sim:
                best_sim = sim
                best_match = spk

        if best_match is not None and best_sim >= self.merge_threshold:
            # 判定为同一说话人，建立别名映射并合并质心
            target = best_match
            self._aliases[speaker_key] = target
            self._aliases[canonical] = target

            # 更新 target 质心
            old_c = self._centroids[target]
            old_n = self._counts[target]
            new_c = (old_c * old_n + unit_emb) / (old_n + 1)
            new_norm = np.linalg.norm(new_c)
            self._centroids[target] = (new_c / max(new_norm, 1e-6)).astype(np.float32)
            self._counts[target] = old_n + 1
            return target

        # 否则更新本说话人质心
        if canonical not in self._centroids:
            self._centroids[canonical] = unit_emb
            self._counts[canonical] = 1
        else:
            old_c = self._centroids[canonical]
            old_n = self._counts[canonical]
            new_c = (old_c * old_n + unit_emb) / (old_n + 1)
            new_norm = np.linalg.norm(new_c)
            self._centroids[canonical] = (new_c / max(new_norm, 1e-6)).astype(np.float32)
            self._counts[canonical] = old_n + 1

        return canonical

    @property
    def speaker_count(self) -> int:
        return len(self._centroids)


class AHCClusterer:
    """会后全局层次凝聚聚类 (AHC) 二次修正器。"""

    def __init__(
        self,
        distance_threshold: float = 0.35,
        max_speakers: int = 4,
    ) -> None:
        self.distance_threshold = distance_threshold
        self.max_speakers = max(1, min(max_speakers, 8))

    def cluster_speakers(
        self, speaker_embeddings: Sequence[tuple[str, np.ndarray]]
    ) -> dict[str, str]:
        """对全量说话人段落声纹执行 AHC 聚类。

        返回 {old_speaker_key: canonical_speaker_key} 重映射字典。
        """
        if not speaker_embeddings:
            return {}

        # 按 speaker_key 聚合声纹向量
        by_speaker: dict[str, list[np.ndarray]] = defaultdict(list)
        for spk_key, emb in speaker_embeddings:
            norm = np.linalg.norm(emb)
            if norm >= 1e-6:
                by_speaker[spk_key].append((emb / norm).astype(np.float32))

        unique_speakers = list(by_speaker.keys())
        if len(unique_speakers) <= 1:
            return {}

        # 计算每个原始 speaker 的质心向量
        centroids: list[np.ndarray] = []
        speaker_weights: list[int] = []
        for spk in unique_speakers:
            embs = by_speaker[spk]
            mean_v = np.mean(embs, axis=0)
            norm = np.linalg.norm(mean_v)
            centroids.append((mean_v / max(norm, 1e-6)).astype(np.float32))
            speaker_weights.append(len(embs))

        k = len(unique_speakers)
        # 构建余弦距离矩阵
        distance_matrix = np.zeros((k, k), dtype=np.float32)
        for i in range(k):
            for j in range(i + 1, k):
                sim = _cosine_similarity(centroids[i], centroids[j])
                dist = max(0.0, 1.0 - sim)
                distance_matrix[i, j] = dist
                distance_matrix[j, i] = dist

        from scipy.cluster.hierarchy import fcluster, linkage  # type: ignore[import-untyped]
        from scipy.spatial.distance import squareform  # type: ignore[import-untyped]

        condensed_dist = squareform(distance_matrix)
        linkage_matrix = linkage(condensed_dist, method="average")

        # 1. 距离阈值聚类
        cluster_labels = fcluster(
            linkage_matrix, t=self.distance_threshold, criterion="distance"
        )

        # 2. 如果聚类簇数仍然超过 max_speakers，强制约束聚类上限
        unique_clusters = len(set(cluster_labels))
        if unique_clusters > self.max_speakers:
            cluster_labels = fcluster(
                linkage_matrix, t=self.max_speakers, criterion="maxclust"
            )

        # 构建重映射表
        clusters_to_speakers: dict[int, list[tuple[str, int]]] = defaultdict(list)
        for idx, (spk, weight) in enumerate(zip(unique_speakers, speaker_weights, strict=True)):
            cid = int(cluster_labels[idx])
            clusters_to_speakers[cid].append((spk, weight))

        remapping: dict[str, str] = {}
        for spk_list in clusters_to_speakers.values():
            if len(spk_list) <= 1:
                continue
            # 选取权重最大（出现段落最多）的 speaker_key 作为簇主键
            sorted_spks = sorted(spk_list, key=lambda x: x[1], reverse=True)
            canonical_key = sorted_spks[0][0]
            for spk_key, _ in sorted_spks[1:]:
                remapping[spk_key] = canonical_key

        return remapping


class AudioMemoryBuffer:
    """会议纯内存音频缓冲（严格不落盘，会后随会话销毁）。"""

    def __init__(self, sample_rate: int = 16000) -> None:
        self.sample_rate = sample_rate
        self.bytes_per_sample = 2  # 16-bit PCM mono
        self.bytes_per_sec = sample_rate * self.bytes_per_sample
        self._chunks: list[bytes] = []
        self._total_bytes = 0

    def append(self, pcm_data: bytes) -> None:
        if not pcm_data:
            return
        self._chunks.append(pcm_data)
        self._total_bytes += len(pcm_data)

    def get_slice(self, start_ms: int, end_ms: int) -> bytes:
        """根据起止毫秒截取 PCM 音频片段。"""
        if end_ms <= start_ms or start_ms < 0:
            return b""
        start_byte = int((start_ms / 1000.0) * self.bytes_per_sec)
        end_byte = int((end_ms / 1000.0) * self.bytes_per_sec)
        start_byte = (start_byte // self.bytes_per_sample) * self.bytes_per_sample
        end_byte = (end_byte // self.bytes_per_sample) * self.bytes_per_sample

        if start_byte >= self._total_bytes:
            return b""

        full_audio = b"".join(self._chunks)
        return full_audio[start_byte:end_byte]

    def clear(self) -> None:
        self._chunks.clear()
        self._total_bytes = 0

    @property
    def duration_secs(self) -> float:
        return (
            self._total_bytes / float(self.bytes_per_sec)
            if self.bytes_per_sec > 0
            else 0.0
        )


class MeetingVoiceprintManager:
    """会议声纹生命周期管理：实时在线质心更新与会后全局聚类。"""

    def __init__(
        self,
        extractor: CAMPlusExtractor | None = None,
        centroid_pool: CentroidPool | None = None,
        clusterer: AHCClusterer | None = None,
        *,
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self.extractor = extractor
        self.centroid_pool = centroid_pool or CentroidPool()
        self.clusterer = clusterer or AHCClusterer()
        self.audio_buffer = AudioMemoryBuffer()
        self._segment_embeddings: list[tuple[str, np.ndarray]] = []
        self._processed_segment_ids: set[Any] = set()

    def append_audio(self, pcm_chunk: bytes) -> None:
        if not self.enabled:
            return
        self.audio_buffer.append(pcm_chunk)

    def process_segments(self, segments: Sequence[Any]) -> None:
        """为新增的确认段落提取声纹并更新在线质心池。"""
        if not self.enabled or self.extractor is None:
            return

        for seg in segments:
            seg_id = getattr(seg, "id", None)
            if seg_id is not None and seg_id in self._processed_segment_ids:
                continue

            start_ms = getattr(seg, "start_ms", 0)
            end_ms = getattr(seg, "end_ms", 0)
            duration_ms = end_ms - start_ms
            if duration_ms < 500:
                continue

            audio_slice = self.audio_buffer.get_slice(start_ms, end_ms)
            if not audio_slice:
                continue

            emb = self.extractor.extract_embedding(audio_slice)
            if emb is not None:
                if seg_id is not None:
                    self._processed_segment_ids.add(seg_id)
                speaker_key = getattr(seg, "speaker_key", "")
                if speaker_key:
                    self._segment_embeddings.append((speaker_key, emb))
                    self.centroid_pool.add_embedding(speaker_key, emb)

    def compute_global_remapping(self, max_speakers: int = 4) -> dict[str, str]:
        """会后执行全局 AHC 聚类，返回合并重映射。"""
        if not self.enabled or not self._segment_embeddings:
            return {}

        self.clusterer.max_speakers = max(1, min(max_speakers, 8))
        return self.clusterer.cluster_speakers(self._segment_embeddings)

    def match_enrolled_speakers(
        self, matcher: VoiceprintProfileMatcher
    ) -> dict[str, str]:
        """对当前所有活跃质心进行 1:N 库匹配，返回 {speaker_key: display_name}。"""
        if not self.enabled:
            return {}
        matched_names: dict[str, str] = {}
        for spk_key, centroid in self.centroid_pool._centroids.items():
            match_res = matcher.match_centroid(centroid)
            if match_res is not None:
                _spk_id, display_name, _sim = match_res
                matched_names[spk_key] = display_name
        return matched_names

    def clear(self) -> None:
        self.audio_buffer.clear()
        self._segment_embeddings.clear()
        self._processed_segment_ids.clear()


@dataclass(frozen=True, slots=True)
class EnrolledSpeaker:
    speaker_id: str
    display_name: str
    embedding: np.ndarray


class VoiceprintProfileMatcher:
    """已知说话人 1:N 声纹比对与自动命名匹配器。"""

    def __init__(self, match_threshold: float = 0.72) -> None:
        self.match_threshold = match_threshold
        self._enrolled: dict[str, EnrolledSpeaker] = {}

    def enroll(self, speaker_id: str, display_name: str, embedding: np.ndarray) -> None:
        norm = float(np.linalg.norm(embedding))
        if norm >= 1e-6:
            unit_emb = (embedding / norm).astype(np.float32)
            self._enrolled[speaker_id] = EnrolledSpeaker(
                speaker_id=speaker_id,
                display_name=display_name,
                embedding=unit_emb,
            )

    def match_centroid(self, centroid: np.ndarray) -> tuple[str, str, float] | None:
        """比对声纹质心与已注册声纹库，返回 (speaker_id, display_name, similarity) 或 None。"""
        norm = float(np.linalg.norm(centroid))
        if norm < 1e-6 or not self._enrolled:
            return None

        unit_c = (centroid / norm).astype(np.float32)
        best_match: EnrolledSpeaker | None = None
        best_sim = -1.0

        for enrolled in self._enrolled.values():
            sim = _cosine_similarity(unit_c, enrolled.embedding)
            if sim > best_sim:
                best_sim = sim
                best_match = enrolled

        if best_match is not None and best_sim >= self.match_threshold:
            return best_match.speaker_id, best_match.display_name, best_sim
        return None


