"""WG7 の試験評価で参加者に配る、全員共通のリンクを出す。

  python tools/wg_links.py

人の見分けは、画面の帯に出る端末ごとの ID（4文字）で行う。ID と名前の対応表はこのリポジトリに置かない（公開されるため）。
"""

import argparse

LINK = "https://akii-ai.github.io/guideline-figure-search/?wg=7"


def main():
    argparse.ArgumentParser(description="WG7 の参加者に配る共通のリンク").parse_args()
    print(LINK)


if __name__ == "__main__":
    main()
