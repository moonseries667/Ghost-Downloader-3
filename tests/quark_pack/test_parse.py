from __future__ import annotations

from quark_pack.parse import QuarkShareRef, extractShareEntries, parseShareUrl, toCanonicalShareUrl


def test_parse_share_url_reads_plain_pwd_and_hash():
    assert parseShareUrl("https://pan.quark.cn/s/plain001") == QuarkShareRef("plain001", "")
    assert parseShareUrl(" http://www.pan.quark.cn/s/AbC123 ") == QuarkShareRef("AbC123", "")
    assert parseShareUrl("https://pan.quark.cn/s/pwd00001?pwd=ab12") == QuarkShareRef("pwd00001", "ab12")
    assert parseShareUrl("https://pan.quark.cn/s/hash9999#/list/share") == QuarkShareRef("hash9999", "")
    assert parseShareUrl(
        "https://pan.quark.cn/s/hash9999?pwd=ab12#/list/share/folder",
    ) == QuarkShareRef("hash9999", "ab12")


def test_extract_pairs_official_passcode():
    text = (
        "我用夸克网盘分享了「示例文件」，点击链接即可保存。\n"
        "链接：https://pan.quark.cn/s/shareid01\n"
        "提取码：ab12\n"
    )
    assert extractShareEntries(text) == [QuarkShareRef("shareid01", "ab12")]


def test_extract_keeps_passcodes_on_their_own_links():
    text = (
        "https://pan.quark.cn/s/aaaa1111\n"
        "https://pan.quark.cn/s/bbbb2222\n"
        "提取码：wxyz\n"
        "https://pan.quark.cn/s/cccc3333?pwd=q1w2\n"
        "https://pan.quark.cn/s/dddd4444 复制参数 ?pwd=k1m2\n"
    )
    assert extractShareEntries(text) == [
        QuarkShareRef("aaaa1111", ""),
        QuarkShareRef("bbbb2222", "wxyz"),
        QuarkShareRef("cccc3333", "q1w2"),
        QuarkShareRef("dddd4444", "k1m2"),
    ]


def test_extract_dedupes_pwd_id_and_keeps_first_order():
    text = (
        "https://pan.quark.cn/s/dup00001?pwd=aaaa\n"
        "https://pan.quark.cn/s/uniq0002\n"
        "https://pan.quark.cn/s/uniq0003\n提取码：bbbb\n"
        "https://pan.quark.cn/s/dup00001\n提取码：zzzz\n"
    )
    assert extractShareEntries(text) == [
        QuarkShareRef("dup00001", "aaaa"),
        QuarkShareRef("uniq0002", ""),
        QuarkShareRef("uniq0003", "bbbb"),
    ]


def test_extract_ignores_passcode_outside_window():
    text = "https://pan.quark.cn/s/far00001\n" + ("x" * 80) + "提取码：qwer"
    assert extractShareEntries(text) == [QuarkShareRef("far00001", "")]


def test_parse_rejects_unrelated_text_without_raising():
    samples = ["", "乱码@@@", "https://pan.baidu.com/s/abc", "ftp://pan.quark.cn/s/abc"]
    for sample in samples:
        assert parseShareUrl(sample) is None
        assert extractShareEntries(sample) == []


def test_canonical_url_roundtrip():
    withPasscode = parseShareUrl("https://pan.quark.cn/s/hash9999?pwd=ab12#/list/share")
    assert withPasscode is not None
    assert toCanonicalShareUrl(withPasscode) == "https://pan.quark.cn/s/hash9999?pwd=ab12"
    assert parseShareUrl(toCanonicalShareUrl(withPasscode)) == withPasscode

    plain = parseShareUrl("https://www.pan.quark.cn/s/plain001#/list/share")
    assert plain is not None
    assert toCanonicalShareUrl(plain) == "https://pan.quark.cn/s/plain001"
    assert parseShareUrl(toCanonicalShareUrl(plain)) == plain
