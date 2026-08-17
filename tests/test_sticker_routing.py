'''
author:        captain-wangrun-cn <wangrun114514@foxmail.com>
date:          2026-08-17 00:00:00
Copyright © WR（captain-wangrun-cn） All rights reserved
'''
"""表情包的路由归属：表情包**不算**"图片输入"，不能被路由到后脑。

设计是：表情包走 fast-image 生成一句话 desc 带进**前脑**，真图不进主图模型
（`core/back_brain.py` 里按 `is_sticker` 过滤掉）。

历史 bug（2026-08-17 生产复现）：`image_input_detected = bool(multimodal_images)`
把表情包也算成图片输入，于是纯表情包消息被路由到后脑、跳过整段前脑，而后脑拿到的
图又刚好被过滤空——两头落空：前脑没走、后脑没图，退化成一轮没有任何图片的纯文本
coder 轮。表现是问"这个表情包是什么内容"时回一句"具体画面我看不到"，
且日志里是 `Turn 1 模型: coder` 而不是 `image`。

这里用源码级断言而不是跑完整 handle_new_message：那条链路要 DB / LLM / presence
一大套依赖，而这个 bug 的本质就是**一个布尔量的取值来源**，源码断言能精确钉住它。
"""
import inspect
import re

from core.message_handler import MessageHandlerMixin


def _routing_source() -> str:
    return inspect.getsource(MessageHandlerMixin.handle_new_message)


def test_image_input_detected_excludes_stickers():
    """image_input_detected 必须排除表情包，否则纯表情包消息会被路由到后脑。"""
    source = _routing_source()
    match = re.search(r"^\s*image_input_detected\s*=\s*(.+)$", source, re.MULTILINE)
    assert match, "找不到 image_input_detected 的赋值"
    rhs = match.group(1).strip()
    # 关键：不能再是裸的 has_real_image / bool(multimodal_images)
    assert rhs != "has_real_image", (
        "image_input_detected 不能直接用 has_real_image——那会把表情包算成图片输入"
    )
    assert "bool(multimodal_images)" not in rhs
    assert "non_sticker" in rhs, f"应基于排除表情包后的图片判断，实际: {rhs}"


def test_non_sticker_flag_filters_by_is_sticker():
    """排除表情包的判断必须真的看 is_sticker 标记。"""
    source = _routing_source()
    match = re.search(
        r"has_real_non_sticker_image\s*=\s*(.*?)\n\s*#", source, re.DOTALL
    )
    assert match, "找不到 has_real_non_sticker_image 的赋值"
    expr = match.group(1)
    assert "is_sticker" in expr
    assert "multimodal_images" in expr


def test_load_failure_check_still_uses_all_images():
    """"标记有但没加载到"要看**含表情包**的 has_real_image。

    否则一个成功加载的表情包会被误判成"图片加载失败"，
    给模型注入 image_load_failed，让它以为用户发图失败了。
    """
    source = _routing_source()
    assert "if has_image_marker and not has_real_image:" in source


def test_multimodal_images_still_carries_stickers():
    """context 里必须仍然带着表情包——前脑要靠它生成 desc。

    如果为了修路由把 sticker 从 multimodal_images 里摘掉，
    `_describe_stickers` 就拿不到任何东西，desc 永远为空。
    """
    source = _routing_source()
    # 回填 context 时不该按 is_sticker 过滤
    assert 'context["multimodal_images"] = multimodal_images' in source


def test_front_brain_path_injects_sticker_desc():
    """前脑路径必须就地生成 desc。

    表情包现在走前脑，而后脑那套注入的条件是 `not message_saved`；
    前脑路径马上就要把 `_message_saved` 置 True，所以不在这里生成就永远没有 desc。
    """
    source = _routing_source()
    front_brain_idx = source.find("前脑优先路由")
    assert front_brain_idx > 0, "找不到前脑路由段"
    front_brain_section = source[front_brain_idx:]
    assert "_describe_stickers" in front_brain_section, (
        "前脑路径没有生成表情包描述——前脑只会看到一个裸标记"
    )


def test_front_brain_desc_written_back_to_context_text():
    """desc 要同时回填 context["text"]，否则入库有 desc、当轮前脑看不到。"""
    source = _routing_source()
    front_brain_section = source[source.find("前脑优先路由"):]
    assert 'context["text"]' in front_brain_section
