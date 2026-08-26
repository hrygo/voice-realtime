import type {
  ReadingBlockOptions,
  TranscriptSegment,
  TranscriptViewBlock,
} from "../../contracts/meetingContract";

export const DEFAULT_READING_OPTIONS: Required<ReadingBlockOptions> = {
  maxGapMs: 1200,
  maxDurationMs: 15000,
  maxLength: 180,
};

function shouldInsertSpaceBetween(prevText: string, nextText: string): boolean {
  if (!prevText || !nextText) return false;
  const lastChar = prevText.slice(-1);
  const firstChar = nextText.charAt(0);
  const isAsciiWord = (ch: string) => /[a-zA-Z0-9]/.test(ch);
  return isAsciiWord(lastChar) && isAsciiWord(firstChar);
}

/**
 * 将平铺的 TranscriptSegment[] 派生为 TranscriptViewBlock[] 阅读块 (§5.1, §6.2)
 *
 * 合并原则：
 * 1. 相同 speaker_key 且相同 source_epoch
 * 2. 中间停顿 gap <= maxGapMs (默认 1200ms)
 * 3. 合并后总时长 <= maxDurationMs (默认 15000ms)
 * 4. 合并后总字数 <= maxLength (默认 180 字)
 * 5. 绝对不跨说话人或跨 epoch 合并
 */
export function deriveReadingBlocks(
  segments: readonly TranscriptSegment[],
  starredIds?: ReadonlySet<string>,
  options?: ReadingBlockOptions,
): TranscriptViewBlock[] {
  if (!segments || segments.length === 0) {
    return [];
  }

  const { maxGapMs, maxDurationMs, maxLength } = {
    ...DEFAULT_READING_OPTIONS,
    ...options,
  };

  const blocks: TranscriptViewBlock[] = [];
  let currentBlock: {
    block_id: string;
    segment_ids: string[];
    speaker_key: string;
    speaker_name: string;
    source_epoch?: number;
    start_ms: number;
    end_ms: number;
    text: string;
    isStarred: boolean;
  } | null = null;

  for (const seg of segments) {
    const isSegStarred = Boolean(starredIds?.has(seg.id));

    if (!currentBlock) {
      currentBlock = {
        block_id: `block-${seg.id}`,
        segment_ids: [seg.id],
        speaker_key: seg.speaker_key,
        speaker_name: seg.speaker_name,
        source_epoch: seg.source_epoch,
        start_ms: seg.start_ms,
        end_ms: seg.end_ms,
        text: seg.text,
        isStarred: isSegStarred,
      };
      continue;
    }

    const sameSpeaker = seg.speaker_key === currentBlock.speaker_key;
    const sameEpoch = (seg.source_epoch ?? 0) === (currentBlock.source_epoch ?? 0);
    const gapMs = Math.max(0, seg.start_ms - currentBlock.end_ms);
    const newDurationMs = seg.end_ms - currentBlock.start_ms;
    const separator = shouldInsertSpaceBetween(currentBlock.text, seg.text) ? " " : "";
    const newTextLength = currentBlock.text.length + separator.length + seg.text.length;

    const canMerge =
      sameSpeaker &&
      sameEpoch &&
      gapMs <= maxGapMs &&
      newDurationMs <= maxDurationMs &&
      newTextLength <= maxLength;

    if (canMerge) {
      currentBlock.segment_ids.push(seg.id);
      currentBlock.end_ms = Math.max(currentBlock.end_ms, seg.end_ms);
      currentBlock.text = `${currentBlock.text}${separator}${seg.text}`;
      if (isSegStarred) {
        currentBlock.isStarred = true;
      }
    } else {
      blocks.push({
        ...currentBlock,
        segment_ids: [...currentBlock.segment_ids],
      });
      currentBlock = {
        block_id: `block-${seg.id}`,
        segment_ids: [seg.id],
        speaker_key: seg.speaker_key,
        speaker_name: seg.speaker_name,
        source_epoch: seg.source_epoch,
        start_ms: seg.start_ms,
        end_ms: seg.end_ms,
        text: seg.text,
        isStarred: isSegStarred,
      };
    }
  }

  if (currentBlock) {
    blocks.push({
      ...currentBlock,
      segment_ids: [...currentBlock.segment_ids],
    });
  }

  return blocks;
}

/**
 * 根据 block 的 segment_ids 从原始 segments 列表中还原片段
 */
export function getSegmentsForBlock(
  block: TranscriptViewBlock,
  allSegments: readonly TranscriptSegment[],
): TranscriptSegment[] {
  const idSet = new Set(block.segment_ids);
  return allSegments.filter((seg) => idSet.has(seg.id));
}
