#!/usr/bin/env bash
# 幂等安装 NLTK punkt_tab 数据（pipecat TTS 断句依赖，缺失时每轮抛 ErrorFrame）。
# 用法：scripts/install-nltk-data.sh
set -euo pipefail

URL="https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/tokenizers/punkt_tab.zip"
DEST="${HOME}/nltk_data/tokenizers"

if [[ -d "${DEST}/punkt_tab/english" ]]; then
    echo "punkt_tab 已安装: ${DEST}/punkt_tab/english"
    exit 0
fi

mkdir -p "${DEST}"
echo "下载 punkt_tab → ${DEST} …"
curl -fsSL -o "${DEST}/punkt_tab.zip" "${URL}"
python3 -c "import zipfile; zipfile.ZipFile('${DEST}/punkt_tab.zip').extractall('${DEST}')"
rm -f "${DEST}/punkt_tab.zip"
echo "完成: $(ls "${DEST}/punkt_tab/english")"