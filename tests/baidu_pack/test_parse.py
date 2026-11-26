from __future__ import annotations

from baidu_pack.parse import (
    BaiduShareRef,
    extractShareEntries,
    parseShareUrl,
    toCanonicalShareUrl,
    toShareBaseUrl,
)


def test_parse_share_url_supports_pan_yun_and_init_formats():
    ref1 = parseShareUrl("https://pan.baidu.com/s/1AbCdEf123456")
    assert ref1 == BaiduShareRef("1AbCdEf123456", "")
    assert ref1.shortKey == "AbCdEf123456"

    ref2 = parseShareUrl("https://yun.baidu.com/s/1XyZ987654?pwd=ab12")
    assert ref2 == BaiduShareRef("1XyZ987654", "ab12")

    ref3 = parseShareUrl("https://pan.baidu.com/share/init?surl=InitKey99&pwd=9k8m")
    assert ref3 == BaiduShareRef("1InitKey99", "9k8m")
    assert ref3.shortKey == "InitKey99"

    assert parseShareUrl("https://example.com/s/1AbCdEf") is None
    assert parseShareUrl("https://pan.baidu.com/s/1AbCdEf 提取码: ab12") is None


def test_extract_share_entries_handles_chinese_share_copy_and_dedup():
    text = (
        "通过百度网盘分享的文件：资料合集\n"
        "链接：https://pan.baidu.com/s/1Alpha111111\n"
        "提取码：a1b2\n"
        "复制这段内容打开百度网盘手机App，操作更方便哦\n\n"
        "重复链接 https://yun.baidu.com/s/1Alpha111111?pwd=xxxx\n"
        "第二条：https://pan.baidu.com/share/init?surl=Beta222222 密码: c3d4\n"
        "第三条无码：https://pan.baidu.com/s/1Gamma333333\n"
        + ("填充文字" * 30)
        + "提取码：zzzz\n"
    )
    entries = extractShareEntries(text)
    assert entries == [
        BaiduShareRef("1Alpha111111", "a1b2"),
        BaiduShareRef("1Beta222222", "c3d4"),
        BaiduShareRef("1Gamma333333", ""),
    ]


def test_to_canonical_and_base_urls():
    ref = BaiduShareRef("1DemoShare01", "704e")
    assert toShareBaseUrl(ref.shareKey) == "https://pan.baidu.com/s/1DemoShare01"
    assert toCanonicalShareUrl(ref) == "https://pan.baidu.com/s/1DemoShare01?pwd=704e"
    assert toCanonicalShareUrl(BaiduShareRef("1DemoShare01", "")) == "https://pan.baidu.com/s/1DemoShare01"
