from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QWidget

from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    MessageBoxBase,
    PixmapLabel,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    SettingCard,
    SubtitleLabel,
)

if TYPE_CHECKING:
    from .account import QuarkAccount


def toQrPixmap(content: str, size: int = 240) -> QPixmap:
    import qrcode
    from qrcode.image.pure import PyPNGImage

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2, box_size=10)
    qr.add_data(content)
    qr.make(fit=True)
    buf = BytesIO()
    qr.make_image(image_factory=PyPNGImage).save(buf)
    pixmap = QPixmap()
    pixmap.loadFromData(buf.getvalue(), "PNG")
    return pixmap.scaled(size, size)


class QuarkScanLoginDialog(MessageBoxBase):
    def __init__(self, account: QuarkAccount, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._account = account
        self._loginUrl = ""

        self._initWidget()
        self._initLayout()
        self._bind()
        self.reloadQrCode()

    def _initWidget(self) -> None:
        self.widget.setFixedSize(430, 560)
        self.yesButton.hide()
        self.cancelButton.setText(self.tr("关闭"))

        self.titleLabel = SubtitleLabel(self.tr("扫码登录"), self.widget)
        self.descriptionLabel = CaptionLabel(
            self.tr("使用夸克 App 扫描下方二维码，并在手机端确认登录"),
            self.widget,
        )
        self.qrLabel = PixmapLabel(self.widget)
        self.qrLabel.setFixedSize(240, 240)
        self.qrLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qrLabel.setScaledContents(True)

        self.statusLabel = BodyLabel(self.tr("正在获取二维码..."), self.widget)
        self.statusLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.statusLabel.setWordWrap(True)
        self.tipLabel = CaptionLabel(
            self.tr('二维码有效期约 180 秒，失效后可点击"刷新二维码"重新生成'),
            self.widget,
        )
        self.tipLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tipLabel.setWordWrap(True)

        self.refreshButton = PrimaryPushButton(FluentIcon.SYNC, self.tr("刷新二维码"), self.widget)
        self.openBrowserButton = PushButton(FluentIcon.LINK, self.tr("打开登录链接"), self.widget)
        self.openBrowserButton.setEnabled(False)

    def _initLayout(self) -> None:
        buttonLayout = QHBoxLayout()
        buttonLayout.setSpacing(12)
        buttonLayout.addWidget(self.refreshButton)
        buttonLayout.addWidget(self.openBrowserButton)

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.descriptionLabel)
        self.viewLayout.addSpacing(12)
        self.viewLayout.addWidget(self.qrLabel, 0, Qt.AlignmentFlag.AlignCenter)
        self.viewLayout.addSpacing(8)
        self.viewLayout.addWidget(self.statusLabel)
        self.viewLayout.addWidget(self.tipLabel)
        self.viewLayout.addSpacing(6)
        self.viewLayout.addLayout(buttonLayout)

    def _bind(self) -> None:
        self.refreshButton.clicked.connect(self.reloadQrCode)
        self.openBrowserButton.clicked.connect(self._onOpenBrowser)
        self._account.qrStateChanged.connect(self._onQrState, owner=self)

    def reloadQrCode(self) -> None:
        self.qrLabel.setPixmap(QPixmap())
        self.qrLabel.setFixedSize(240, 240)
        self.statusLabel.setText(self.tr("正在获取二维码..."))
        self.openBrowserButton.setEnabled(False)
        self._loginUrl = ""
        self._account.startQrLogin()

    def _onQrState(self, statusCode: int, message: str) -> None:
        from .account import QR_EXPIRED, QR_GOT_URL, QR_LOGIN_SUCCESS, QR_SCANNED, QR_UNSCANNED

        if statusCode == QR_LOGIN_SUCCESS:
            self.statusLabel.setText(self.tr("登录成功"))
            self.accept()
        elif statusCode == QR_GOT_URL:
            self._loginUrl = message
            self.qrLabel.setPixmap(toQrPixmap(message))
            self.qrLabel.setFixedSize(240, 240)
            self.statusLabel.setText(self.tr("请使用夸克 App 扫码登录"))
            self.openBrowserButton.setEnabled(True)
        elif statusCode == QR_UNSCANNED:
            self.statusLabel.setText(self.tr("等待扫码"))
        elif statusCode == QR_SCANNED:
            self.statusLabel.setText(self.tr("已扫码，请在手机上确认"))
        elif statusCode == QR_EXPIRED:
            self.statusLabel.setText(self.tr('二维码已失效，请点击"刷新二维码"重新生成'))
        else:
            self.statusLabel.setText(self._toDisplayMessage(statusCode, message))

    def _toDisplayMessage(self, statusCode: int, message: str) -> str:
        from .client import toMaskedCookie

        text = message or str(statusCode)
        cookie = self._account.cookie
        if cookie and cookie in text:
            text = text.replace(cookie, toMaskedCookie(cookie) or "****")
        return text

    def _onOpenBrowser(self) -> None:
        if self._loginUrl:
            QDesktopServices.openUrl(QUrl(self._loginUrl))

    def done(self, code: int) -> None:
        self._account.cancelQrLogin()
        super().done(code)


class QuarkCookieDialog(MessageBoxBase):
    def __init__(self, parent: QWidget | None = None, initialCookie: str = "") -> None:
        super().__init__(parent)
        self._initialCookie = initialCookie or ""

        self._initWidget()
        self._initLayout()

    def _initWidget(self) -> None:
        self.widget.setFixedSize(420, 500)
        self.yesButton.setText(self.tr("保存"))
        self.cancelButton.setText(self.tr("取消"))

        self.titleLabel = SubtitleLabel(self.tr("手动导入 Cookie"), self.widget)
        self.descriptionLabel = CaptionLabel(
            self.tr("请粘贴浏览器导出的完整 Cookie，留空保存会清空当前 Cookie"), self.widget,
        )
        self.cookieTextEdit = PlainTextEdit(self.widget)
        self.cookieTextEdit.setPlaceholderText(self.tr("请在此输入用户 Cookie"))
        self.cookieTextEdit.setPlainText(self._initialCookie)

    def _initLayout(self) -> None:
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.descriptionLabel)
        self.viewLayout.addWidget(self.cookieTextEdit)


class QuarkLoginSettingCard(SettingCard):
    def __init__(self, account: QuarkAccount, parent: QWidget | None = None) -> None:
        self._account = account
        super().__init__(
            FluentIcon.VIEW, self.tr("账号登录"),
            self.tr("状态：未登录"), parent,
        )

        self._initWidget()
        self._initLayout()
        self._bind()
        self.refreshLoginInfo()

    def _initWidget(self) -> None:
        self.scanButton = PrimaryPushButton(self.tr("扫码登录"), self)
        self.editButton = PushButton(self.tr("导入 Cookie"), self)
        self.refreshButton = PushButton(self.tr("刷新状态"), self)
        self.logoutButton = PushButton(self.tr("退出登录"), self)

    def _initLayout(self) -> None:
        self.hBoxLayout.addWidget(self.scanButton, 0)
        self.hBoxLayout.addSpacing(8)
        self.hBoxLayout.addWidget(self.editButton, 0)
        self.hBoxLayout.addSpacing(8)
        self.hBoxLayout.addWidget(self.refreshButton, 0)
        self.hBoxLayout.addSpacing(8)
        self.hBoxLayout.addWidget(self.logoutButton, 0)
        self.hBoxLayout.addSpacing(16)

    def _bind(self) -> None:
        self.scanButton.clicked.connect(self._onScanLogin)
        self.editButton.clicked.connect(self._onEditCookie)
        self.refreshButton.clicked.connect(self._onRefresh)
        self.logoutButton.clicked.connect(self._onLogout)
        self._account.accountChanged.connect(self.refreshLoginInfo, owner=self)

    def refreshLoginInfo(self) -> None:
        nickname = self._account.nickname
        vip = self._account.vip or self.tr("未知")
        if not self._account.isLoggedIn:
            self.setContent(self.tr("状态：未登录"))
        elif not nickname:
            self.setContent(self.tr("状态：已保存 Cookie，但尚未验证或已失效"))
        else:
            self.setContent(
                self.tr("状态：已登录 昵称：{0} 会员状态：{1}").format(nickname, vip)
            )
        self.scanButton.setEnabled(True)
        self.editButton.setEnabled(True)
        self.refreshButton.setEnabled(True)
        self.logoutButton.setEnabled(self._account.isLoggedIn)

    def _onScanLogin(self) -> None:
        dialog = QuarkScanLoginDialog(self._account, self.window())
        dialog.exec()
        dialog.deleteLater()

    def _onEditCookie(self) -> None:
        from .client import toCookie

        dialog = QuarkCookieDialog(self.window(), self._account.cookie)
        if not dialog.exec():
            dialog.deleteLater()
            return

        newCookie = toCookie(dialog.cookieTextEdit.toPlainText())
        dialog.deleteLater()
        if not newCookie:
            self._account.setCookie("")
            return

        self._account.setCookie(newCookie)

    def _onRefresh(self) -> None:
        self._account.fetchAccountInfo()

    def _onLogout(self) -> None:
        self._account.logout()
