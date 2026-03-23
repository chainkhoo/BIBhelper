import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = REPO_ROOT / "packages" / "bib_core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from bib_core import PipelineError, RunOptions, run_pipeline


def build_parser():
    parser = argparse.ArgumentParser(description="BIBhelper CLI")
    parser.add_argument("--input-dir", default=".", help="PDF 输入目录")
    parser.add_argument("--output-dir", default=".", help="结果输出目录")
    parser.add_argument("--non-interactive", action="store_true", help="关闭人工确认，直接按自动任务处理")
    pdf_group = parser.add_mutually_exclusive_group()
    pdf_group.add_argument("--enable-pdf", dest="enable_pdf", action="store_true", help="启用 PDF 转换")
    pdf_group.add_argument("--no-pdf", dest="enable_pdf", action="store_false", help="关闭 PDF 转换")
    parser.set_defaults(enable_pdf=True)
    parser.add_argument("--usd-cny", type=float, default=None, help="手动指定美元兑人民币汇率")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    input_dir = Path(args.input_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    pdf_files = sorted(input_dir.glob("*.pdf"))
    try:
        result = run_pipeline(
            RunOptions(
                input_files=pdf_files,
                workspace_dir=input_dir,
                output_root=output_dir,
                enable_pdf=args.enable_pdf,
                interactive=not args.non_interactive,
                usd_cny_override=args.usd_cny,
            )
        )
    except PipelineError as exc:
        print(f"处理失败: {exc}", file=sys.stderr)
        return 1
    print(f"处理完成，生成 {len(result.artifacts)} 个产物。")
    for artifact in result.artifacts:
        print(f"- {artifact.relative_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
