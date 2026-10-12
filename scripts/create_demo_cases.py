"""Create new fictional app1/app2 cases for the public DEMO."""
from pathlib import Path
from openpyxl import Workbook


def main() -> None:
    destination = Path(__file__).resolve().parent.parent / "runtime-data" / "demo-cases.xlsx"
    destination.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    workbook.properties.creator = "Demo"
    sheet = workbook.active
    sheet.title = "fictional-demo"
    sheet.append(["用例编号", "用例标题", "前置条件", "操作步骤", "预期结果", "优先级", "用例类型"])
    sheet.append(["DEMO-APP1-001", "app1 示例首页展示", "使用自行准备的 app1 示例环境", "打开 app1，进入示例首页", "显示示例首页标题", "P2", "虚构演示"])
    sheet.append(["DEMO-APP2-001", "app2 示例条目保存", "使用自行准备的 app2 示例环境", "打开 app2，新建名为 DEMO-ITEM 的示例条目并保存", "示例列表显示 DEMO-ITEM", "P2", "虚构演示"])
    workbook.save(destination)
    print(f"Created fictional cases: {destination}")


if __name__ == "__main__":
    main()
