# timeline-geotag-cli

`timeline-geotag` は、Android からエクスポートした Google Maps Timeline JSON と写真の撮影時刻を照合し、写真の GPS 位置を補完する Python CLI です。

## セットアップ

Python プロジェクトと依存関係は [uv](https://docs.astral.sh/uv/) で管理します。

### リポジトリをチェックアウトして使う

開発したり、ローカルの変更を反映して使ったりする場合は、リポジトリをチェックアウトして依存関係を同期します。

```bash
git clone https://github.com/mitszo/timeline-geotag-cli.git
cd timeline-geotag-cli
uv sync
uv run timeline-geotag --help
```

以降の例では、この方法で実行する場合は `uv run timeline-geotag` を使います。

### GitHub からツールとしてインストールする

変更せずに利用するだけなら、GitHub のリポジトリから直接インストールできます。

```bash
uv tool install git+https://github.com/mitszo/timeline-geotag-cli.git
timeline-geotag --help
```

この方法でインストールした場合は、以降の `uv run` を除き、`timeline-geotag` として実行します。

## 使い方

通常は写真を変更しない dry-run です。JPG と Panasonic RW2 を対象にします。

```bash
uv run timeline-geotag photos/ timeline.json --recursive --timezone Asia/Tokyo
```

出力には撮影時刻、推定座標、その座標を開く Google Maps URL、推定元（`visit`、`interpolated`、`nearest`、`no-match`）を表示します。Timeline の visit 区間を優先し、次に時間・距離の上限内の path point 間を補間します。

GPS を書き込むには `--write` を明示します。既存 GPS は既定で維持されます。

```bash
uv run timeline-geotag photos/ timeline.json --recursive --timezone Asia/Tokyo --write
```

`exiftool` が必要です。書込み時は既定で exiftool の `_original` バックアップを作ります。不要な場合だけ `--no-backup` を指定してください。

## Bash completion

```bash
source <(uv run timeline-geotag --completion bash)
```

インストール済みの CLI では `uv run` を除いて実行できます。

```bash
source <(timeline-geotag --completion bash)
```
