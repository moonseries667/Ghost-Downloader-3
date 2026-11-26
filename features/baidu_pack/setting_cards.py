from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    MessageBox,
    MessageBoxBase,
    PasswordLineEdit,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    SettingCard,
    SubtitleLabel,
)

from .account import (
    QR_STATE_EXPIRED,
    QR_STATE_FAILED,
    QR_STATE_LOADING,
    QR_STATE_READY,
    QR_STATE_SCANNED,
    QR_STATE_SUCCESS,
    QR_STATE_WAITING,
)

if TYPE_CHECKING:
    from .account import BaiduAccount


def toImagePixmap(imageBytes: bytes, size: int = 240) -> QPixmap:
    pixmap = QPixmap()
    if not imageBytes or not pixmap.loadFromData(imageBytes):
        return QPixmap()
    return pixmap.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


class BaiduScanLoginDialog(MessageBoxBase):
    def __init__(self, account: BaiduAccount, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._account = account
        self._hasImage = False

        self._initWidget()
        self._initLayout()
        self._bind()
        self.reloadQrCode()

    def _initWidget(self) -> None:
        self.widget.setFixedSize(430, 540)
        self.yesButton.hide()
        self.cancelButton.setText(self.tr("关闭"))

        self.titleLabel = SubtitleLabel(self.tr("扫码登录百度网盘"), self.widget)
        self.descriptionLabel = CaptionLabel(
            self.tr("使用百度网盘 App 或百度 App 扫描下方二维码，并在手机端确认登录"),
            self.widget,
        )
        self.descriptionLabel.setWordWrap(True)

        self.qrLabel = QLabel(self.widget)
        self.qrLabel.setObjectName("baiduQrPixmapLabel")
        self.qrLabel.setFixedSize(240, 240)
        self.qrLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.statusLabel = BodyLabel(self.tr("正在获取百度扫码二维码..."), self.widget)
        self.statusLabel.setObjectName("baiduQrStatusLabel")
        self.statusLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.statusLabel.setWordWrap(True)

        self.tipLabel = CaptionLabel(
            self.tr('扫码确认后会自动写入 BaiduPCS-Go 会话，失效后可点击"刷新二维码"重新生成'),
            self.widget,
        )
        self.tipLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tipLabel.setWordWrap(True)

        self.refreshButton = PrimaryPushButton(FluentIcon.SYNC, self.tr("刷新二维码"), self.widget)
        self.refreshButton.setObjectName("baiduRefreshQrButton")

    def _initLayout(self) -> None:
        self.viewLayout.setSpacing(10)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.descriptionLabel)
        self.viewLayout.addSpacing(8)
        self.viewLayout.addWidget(self.qrLabel, 0, Qt.AlignmentFlag.AlignCenter)
        self.viewLayout.addSpacing(6)
        self.viewLayout.addWidget(self.statusLabel)
        self.viewLayout.addWidget(self.tipLabel)
        self.viewLayout.addStretch(1)
        self.viewLayout.addWidget(self.refreshButton)

    def _bind(self) -> None:
        self.refreshButton.clicked.connect(self.reloadQrCode)
        self._account.qrImageChanged.connect(self._onQrImage, owner=self)
        self._account.qrStateChanged.connect(self._onQrState, owner=self)

    def reloadQrCode(self) -> None:
        self._hasImage = False
        self.qrLabel.setPixmap(QPixmap())
        self.qrLabel.setFixedSize(240, 240)
        self.statusLabel.setText(self.tr("正在获取百度扫码二维码..."))
        self._account.startQrLogin()

    def _onQrImage(self, imageBytes: bytes) -> None:
        pixmap = toImagePixmap(imageBytes, size=240)
        if not pixmap.isNull():
            self._hasImage = True
            self.qrLabel.setPixmap(pixmap)

    def _onQrState(self, state: str, message: str) -> None:
        if state == QR_STATE_SUCCESS:
            self.statusLabel.setText(self.tr("登录成功"))
            self.accept()
            return
        if state == QR_STATE_LOADING:
            self.statusLabel.setText(message or self.tr("正在获取百度扫码二维码..."))
            return
        if state == QR_STATE_READY:
            if not self._hasImage:
                self.statusLabel.setText(self.tr('二维码图片加载失败，请点击"刷新二维码"重试'))
                return
            self.statusLabel.setText(self.tr("请使用百度网盘 App 扫码登录"))
            return
        if state == QR_STATE_WAITING:
            self.statusLabel.setText(message or self.tr("等待手机扫码"))
            return
        if state == QR_STATE_SCANNED:
            self.statusLabel.setText(message or self.tr("已扫码，请在手机端确认登录"))
            return
        if state == QR_STATE_EXPIRED:
            self.statusLabel.setText(message or self.tr('二维码已失效，请点击"刷新二维码"重新生成'))
            return
        self.statusLabel.setText(message or self.tr("扫码登录失败，请刷新二维码重试"))

    def done(self, code: int) -> None:
        self._account.cancelQrLogin()
        super().done(code)


class BaiduCookieDialog(MessageBoxBase):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._initWidget()
        self._initLayout()
        self._bind()

    def _initWidget(self) -> None:
        self.widget.setFixedSize(460, 500)
        self.yesButton.setText(self.tr("登录"))
        self.cancelButton.setText(self.tr("取消"))

        self.titleLabel = SubtitleLabel(self.tr("手动导入百度凭据"), self.widget)
        self.descriptionLabel = CaptionLabel(
            self.tr("可粘贴浏览器导出的完整 Cookie，或单独填写 BDUSS 与 STOKEN（不会明文写入日志）"),
            self.widget,
        )
        self.descriptionLabel.setWordWrap(True)

        self.cookieLabel = CaptionLabel(self.tr("方式一：整段浏览器 Cookie（推荐，需含 BDUSS 与 STOKEN）"), self.widget)
        self.cookieTextEdit = PlainTextEdit(self.widget)
        self.cookieTextEdit.setObjectName("baiduCookieTextEdit")
        self.cookieTextEdit.setPlaceholderText(self.tr("例如：BDUSS=xxx; STOKEN=yyy; BAIDUID=zzz"))
        self.cookieTextEdit.setFixedHeight(140)

        self.credentialsLabel = CaptionLabel(self.tr("方式二：单独填写 BDUSS 与 STOKEN"), self.widget)
        self.bdussEdit = PasswordLineEdit(self.widget)
        self.bdussEdit.setObjectName("baiduBdussEdit")
        self.bdussEdit.setPlaceholderText(self.tr("请输入 BDUSS"))

        self.stokenEdit = PasswordLineEdit(self.widget)
        self.stokenEdit.setObjectName("baiduStokenEdit")
        self.stokenEdit.setPlaceholderText(self.tr("可选：请输入 STOKEN（分享转存必填）"))

    def _initLayout(self) -> None:
        self.viewLayout.setSpacing(8)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.descriptionLabel)
        self.viewLayout.addSpacing(4)
        self.viewLayout.addWidget(self.cookieLabel)
        self.viewLayout.addWidget(self.cookieTextEdit)
        self.viewLayout.addSpacing(6)
        self.viewLayout.addWidget(self.credentialsLabel)
        self.viewLayout.addWidget(self.bdussEdit)
        self.viewLayout.addWidget(self.stokenEdit)

    def _bind(self) -> None:
        pass

    @property
    def rawCookie(self) -> str:
        return self.cookieTextEdit.toPlainText().strip()

    @property
    def bduss(self) -> str:
        return self.bdussEdit.text().strip()

    @property
    def stoken(self) -> str:
        return self.stokenEdit.text().strip()


class BaiduLoginSettingCard(SettingCard):
    def __init__(self, account: BaiduAccount, parent: QWidget | None = None) -> None:
        self._account = account
        super().__init__(
            FluentIcon.VIEW,
            self.tr("账号登录"),
            self.tr("状态：未登录"),
            parent,
        )
        self._initWidget()
        self._initLayout()
        self._bind()
        self.refreshLoginInfo()

    def _initWidget(self) -> None:
        self.scanButton = PrimaryPushButton(self.tr("扫码登录"), self)
        self.scanButton.setObjectName("baiduScanButton")
        self.cookieButton = PushButton(self.tr("导入 Cookie"), self)
        self.cookieButton.setObjectName("baiduCookieButton")
        self.refreshButton = PushButton(self.tr("刷新状态"), self)
        self.refreshButton.setObjectName("baiduRefreshButton")
        self.logoutButton = PushButton(self.tr("退出登录"), self)
        self.logoutButton.setObjectName("baiduLogoutButton")

    def _initLayout(self) -> None:
        self.hBoxLayout.addWidget(self.scanButton, 0)
        self.hBoxLayout.addSpacing(8)
        self.hBoxLayout.addWidget(self.cookieButton, 0)
        self.hBoxLayout.addSpacing(8)
        self.hBoxLayout.addWidget(self.refreshButton, 0)
        self.hBoxLayout.addSpacing(8)
        self.hBoxLayout.addWidget(self.logoutButton, 0)
        self.hBoxLayout.addSpacing(16)

    def _bind(self) -> None:
        self.scanButton.clicked.connect(self._onScanLogin)
        self.cookieButton.clicked.connect(self._onImportCookie)
        self.refreshButton.clicked.connect(self._onRefresh)
        self.logoutButton.clicked.connect(self._onLogout)
        self._account.accountChanged.connect(self.refreshLoginInfo, owner=self)

    def refreshLoginInfo(self) -> None:
        status = self._account.statusText
        error = self._account.lastError
        content = f"{status}（{error}）" if error else status
        self.setContent(content)

        isInstalled = self._account.isInstalled
        isBusy = self._account.isBusy
        self.scanButton.setEnabled(isInstalled and not self._account.isLoggingIn)
        self.cookieButton.setEnabled(isInstalled and not isBusy)
        self.refreshButton.setEnabled(not self._account.isLoggingIn)
        self.logoutButton.setEnabled(isInstalled and self._account.isLoggedIn and not isBusy)

    def _onScanLogin(self) -> None:
        dialog = BaiduScanLoginDialog(self._account, self.window())
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def _onImportCookie(self) -> None:
        dialog = BaiduCookieDialog(self.window())
        try:
            isConfirmed = bool(dialog.exec())
            rawCookie = dialog.rawCookie
            bduss = dialog.bduss
            stoken = dialog.stoken
        finally:
            dialog.deleteLater()
        if not isConfirmed:
            return
        if rawCookie:
            self._account.loginWithCookies(rawCookie)
        elif bduss:
            self._account.loginWithCredentials(bduss, stoken)

    def _onRefresh(self) -> None:
        self._account.refreshAuth()

    def _onLogout(self) -> None:
        dialog = MessageBox(
            self.tr("退出百度网盘登录"),
            self.tr("确定要退出当前 BaiduPCS-Go 账号会话吗？"),
            self.window(),
        )
        try:
            isConfirmed = bool(dialog.exec())
        finally:
            dialog.deleteLater()
        if isConfirmed:
            self._account.logout()
