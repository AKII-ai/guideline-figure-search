"""WG7 の試験評価で参加者に配るリンクの一覧を作る。

  python tools/wg_links.py 8          # WG7-01〜WG7-08 の番号とリンク（タブ区切り。Excel に貼れる）

番号と名前の対応表はこのリポジトリに置かない（公開されるため）。
"""

import argparse
import sys

SITE = "https://akii-ai.github.io/guideline-figure-search/"
MAX_PEOPLE = 999  # app.js が受け取る番号は3桁まで


def links(count):
    return [(f"WG7-{n:02d}", f"{SITE}?wg={n:02d}") for n in range(1, count + 1)]


def main():
    parser = argparse.ArgumentParser(description="WG7 の参加者に配るリンクの一覧")
    parser.add_argument("count", type=int, help="参加者の人数")
    args = parser.parse_args()
    if not 1 <= args.count <= MAX_PEOPLE:
        parser.error(f"人数は 1〜{MAX_PEOPLE} で指定してください。")
    sys.stdout.reconfigure(encoding="utf-8")
    print("参加者番号\tリンク")
    for number, url in links(args.count):
        print(f"{number}\t{url}")


if __name__ == "__main__":
    main()
