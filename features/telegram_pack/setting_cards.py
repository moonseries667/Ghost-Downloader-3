from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    LineEdit,
    MessageBox,
    MessageBoxBase,
    PasswordLineEdit,
    PrimaryPushButton,
    PushButton,
    SettingCard,
    SubtitleLabel,
    ToolTipFilter,
)

from .account import (
    QR_STATE_FAILED,
    QR_STATE_LOADING,
    QR_STATE_NEED_2FA,
    QR_STATE_READY,
    QR_STATE_SUCCESS,
    QR_STATE_VERIFYING_2FA,
)

if TYPE_CHECKING:
    from .account import TelegramAccount


def toQrMatrixPixmap(matrix: tuple[tuple[bool, ...], ...], size: int = 240) -> QPixmap:
    if not matrix:
        return QPixmap()
    dimension = len(matrix)
    moduleImage = QImage(dimension, dimension, QImage.Format.Format_RGB32)
    black = 0xFF000000
    white = 0xFFFFFFFF
    for rowIndex, row in enumerate(matrix):
        for colIndex, isBlack in enumerate(row):
            moduleImage.setPixel(colIndex, rowIndex, black if isBlack else white)

    # 按整数倍缩放保证每个 QR 模块等宽等高，避免非整倍插值造成手机识别偏差
    scale = max(1, size // dimension)
    scaledSize = dimension * scale
    canvasSize = max(size, scaledSize)
    canvas = QImage(canvasSize, canvasSize, QImage.Format.Format_RGB32)
    canvas.fill(white)
    scaled = moduleImage.scaled(
        scaledSize,
        scaledSize,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )
    offset = (canvasSize - scaledSize) // 2
    painter = QPainter(canvas)
    try:
        painter.drawImage(offset, offset, scaled)
    finally:
        painter.end()
    return QPixmap.fromImage(canvas)


class TelegramScanLoginDialog(MessageBoxBase):
    def __init__(self, account: TelegramAccount, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._account = account
        self._initWidget()
        self._initLayout()
        self._bind()
        self.reloadQrCode()

    def _initWidget(self) -> None:
        self.widget.setFixedSize(460, 560)
        self.yesButton.hide()
        self.cancelButton.setText(self.tr("关闭"))

        self.titleLabel = SubtitleLabel(self.tr("扫码登录 Telegram"), self.widget)
        self.descriptionLabel = CaptionLabel(
            self.tr("打开手机 Telegram -> 设置 -> 设备 -> 关联桌面设备，扫描下方二维码"),
            self.widget,
        )
        self.descriptionLabel.setWordWrap(True)

        self.qrLabel = QLabel(self.widget)
        self.qrLabel.setObjectName("telegramQrPixmapLabel")
        self.qrLabel.setFixedSize(240, 240)
        self.qrLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qrLabel.setScaledContents(False)

        self.statusLabel = BodyLabel(self.tr("正在获取登录二维码..."), self.widget)
        self.statusLabel.setObjectName("telegramQrStatusLabel")
        self.statusLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.statusLabel.setWordWrap(True)

        self.twoFaWidget = QWidget(self.widget)
        self.twoFaWidget.setObjectName("telegramTwoFaWidget")
        self.twoFaLabel = CaptionLabel(
            self.tr("该账号开启了两步验证，请输入 2FA 云密码后点击确认登录"),
            self.twoFaWidget,
        )
        self.twoFaLabel.setWordWrap(True)
        self.passwordEdit = PasswordLineEdit(self.twoFaWidget)
        self.passwordEdit.setObjectName("telegramTwoFaEdit")
        self.passwordEdit.setPlaceholderText(self.tr("请输入 2FA 云密码"))
        self.submitPasswordButton = PrimaryPushButton(self.tr("确认登录"), self.twoFaWidget)
        self.submitPasswordButton.setObjectName("telegramSubmitTwoFaButton")
        self.twoFaWidget.hide()

        self.tipLabel = CaptionLabel(
            self.tr('二维码会自动刷新；若长时间未加载，请检查代理配置或点击"刷新二维码"'),
            self.widget,
        )
        self.tipLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tipLabel.setWordWrap(True)

        self.refreshButton = PrimaryPushButton(FluentIcon.SYNC, self.tr("刷新二维码"), self.widget)
        self.refreshButton.setObjectName("telegramRefreshQrButton")

    def _initLayout(self) -> None:
        twoFaRow = QHBoxLayout()
        twoFaRow.setContentsMargins(0, 0, 0, 0)
        twoFaRow.setSpacing(8)
        twoFaRow.addWidget(self.passwordEdit, 1)
        twoFaRow.addWidget(self.submitPasswordButton)

        twoFaLayout = QVBoxLayout(self.twoFaWidget)
        twoFaLayout.setContentsMargins(0, 4, 0, 4)
        twoFaLayout.setSpacing(6)
        twoFaLayout.addWidget(self.twoFaLabel)
        twoFaLayout.addLayout(twoFaRow)

        self.viewLayout.setSpacing(10)
        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.descriptionLabel)
        self.viewLayout.addSpacing(4)
        self.viewLayout.addWidget(self.qrLabel, 0, Qt.AlignmentFlag.AlignCenter)
        self.viewLayout.addWidget(self.twoFaWidget)
        self.viewLayout.addWidget(self.statusLabel)
        self.viewLayout.addWidget(self.tipLabel)
        self.viewLayout.addStretch(1)
        self.viewLayout.addWidget(self.refreshButton)

    def _bind(self) -> None:
        self.refreshButton.clicked.connect(self.reloadQrCode)
        self.submitPasswordButton.clicked.connect(self._onSubmitPassword)
        self.passwordEdit.returnPressed.connect(self._onSubmitPassword)
        self._account.qrMatrixChanged.connect(self._onQrMatrix, owner=self)
        self._account.qrStateChanged.connect(self._onQrState, owner=self)

    def reloadQrCode(self) -> None:
        self.qrLabel.setPixmap(QPixmap())
        self.qrLabel.setFixedSize(240, 240)
        self.qrLabel.show()
        self.passwordEdit.clear()
        self.passwordEdit.setEnabled(True)
        self.submitPasswordButton.setEnabled(True)
        self.twoFaWidget.hide()
        self.statusLabel.setText(self.tr("正在获取登录二维码..."))
        self._account.startQrLogin()

    def _onQrMatrix(self, matrix: tuple[tuple[bool, ...], ...]) -> None:
        if self.twoFaWidget.isVisible():
            return
        self.qrLabel.setPixmap(toQrMatrixPixmap(matrix, size=240))
        self.qrLabel.setFixedSize(240, 240)
        self.qrLabel.show()

    def _onQrState(self, state: str, message: str) -> None:
        if state == QR_STATE_SUCCESS:
            self.statusLabel.setText(message or self.tr("登录成功"))
            self.accept()
            return
        if state == QR_STATE_LOADING:
            self.statusLabel.setText(message or self.tr("正在获取登录二维码..."))
            return
        if state == QR_STATE_READY:
            self.statusLabel.setText(message or self.tr("请使用手机 Telegram 扫描二维码"))
            return
        if state == QR_STATE_NEED_2FA:
            self.qrLabel.hide()
            self.twoFaWidget.show()
            self.passwordEdit.setEnabled(True)
            self.submitPasswordButton.setEnabled(True)
            self.passwordEdit.setFocus()
            self.statusLabel.setText(message or self.tr("请输入两步验证（2FA）云密码"))
            return
        if state == QR_STATE_VERIFYING_2FA:
            self.passwordEdit.setEnabled(False)
            self.submitPasswordButton.setEnabled(False)
            self.statusLabel.setText(message or self.tr("正在校验两步验证密码..."))
            return
        if state == QR_STATE_FAILED:
            self.passwordEdit.setEnabled(False)
            self.submitPasswordButton.setEnabled(False)
            self.statusLabel.setText(message or self.tr("登录失败，请刷新二维码重试"))

    def _onSubmitPassword(self) -> None:
        password = self.passwordEdit.text()
        if not password:
            self.statusLabel.setText(self.tr("请输入 2FA 云密码"))
            self.passwordEdit.setFocus()
            return
        self.passwordEdit.clear()
        if not self._account.submit2faPassword(password):
            self.statusLabel.setText(self.tr("登录会话已结束，请刷新二维码重试"))

    def done(self, code: int) -> None:
        self.passwordEdit.clear()
        self._account.cancelQrLogin()
        super().done(code)


class TelegramDesktopLoginDialog(MessageBoxBase):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._initWidget()
        self._initLayout()
        self._bind()

    def _initWidget(self) -> None:
        self.widget.setMinimumWidth(440)
        self.yesButton.setText(self.tr("开始导入"))
        self.cancelButton.setText(self.tr("取消"))

        self.titleLabel = SubtitleLabel(self.tr("导入 Telegram Desktop 登录态"), self.widget)
        self.descriptionLabel = CaptionLabel(
            self.tr("将打开独立终端窗口完成导入；路径留空则自动探测默认安装目录"),
            self.widget,
        )
        self.descriptionLabel.setWordWrap(True)

        self.pathEdit = LineEdit(self.widget)
        self.pathEdit.setObjectName("desktopPathEdit")
        self.pathEdit.setClearButtonEnabled(True)
        self.pathEdit.setPlaceholderText(self.tr("可选：Telegram Desktop 或 tdata 文件夹路径"))

        self.browseButton = PushButton(FluentIcon.FOLDER, self.tr("浏览"), self.widget)
        self.browseButton.setObjectName("browseDesktopButton")

        self.passcodeLabel = CaptionLabel(
            self.tr("如桌面端设置了本地锁屏密码，请在此填写（不会写入日志）"),
            self.widget,
        )
        self.passcodeEdit = PasswordLineEdit(self.widget)
        self.passcodeEdit.setObjectName("desktopPasscodeEdit")
        self.passcodeEdit.setPlaceholderText(self.tr("可选：本地锁屏密码"))

    def _initLayout(self) -> None:
        pathRow = QHBoxLayout()
        pathRow.setSpacing(8)
        pathRow.addWidget(self.pathEdit, 1)
        pathRow.addWidget(self.browseButton)

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.descriptionLabel)
        self.viewLayout.addSpacing(8)
        self.viewLayout.addLayout(pathRow)
        self.viewLayout.addSpacing(8)
        self.viewLayout.addWidget(self.passcodeLabel)
        self.viewLayout.addWidget(self.passcodeEdit)

    def _bind(self) -> None:
        self.browseButton.clicked.connect(self._onBrowse)

    @property
    def desktopPath(self) -> str:
        return self.pathEdit.text().strip()

    @property
    def passcode(self) -> str:
        return self.passcodeEdit.text().strip()

    def _onBrowse(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self.window(),
            self.tr("选择 Telegram Desktop 或 tdata 目录"),
            self.pathEdit.text().strip(),
        )
        if folder:
            self.pathEdit.setText(folder)


class TelegramLoginSettingCard(SettingCard):
    def __init__(self, account: TelegramAccount, parent: QWidget | None = None) -> None:
        self._account = account
        super().__init__(
            FluentIcon.SEND,
            self.tr("账号登录"),
            self.tr("状态：未登录"),
            parent,
        )
        self._initWidget()
        self._initLayout()
        self._bind()
        self.refreshLoginInfo()

    def _initWidget(self) -> None:
        self.qrButton = PrimaryPushButton(self.tr("扫码登录"), self)
        self.qrButton.setObjectName("telegramQrButton")
        self.qrButton.setToolTip(self.tr("在软件内显示清晰二维码并支持 2FA 云密码验证"))
        self.qrButton.installEventFilter(ToolTipFilter(self.qrButton))

        self.desktopButton = PushButton(self.tr("导入 Desktop"), self)
        self.desktopButton.setObjectName("telegramDesktopButton")

        self.refreshButton = PushButton(self.tr("刷新状态"), self)
        self.refreshButton.setObjectName("telegramRefreshButton")

        self.logoutButton = PushButton(self.tr("退出登录"), self)
        self.logoutButton.setObjectName("telegramLogoutButton")

    def _initLayout(self) -> None:
        self.hBoxLayout.addWidget(self.qrButton, 0)
        self.hBoxLayout.addSpacing(8)
        self.hBoxLayout.addWidget(self.desktopButton, 0)
        self.hBoxLayout.addSpacing(8)
        self.hBoxLayout.addWidget(self.refreshButton, 0)
        self.hBoxLayout.addSpacing(8)
        self.hBoxLayout.addWidget(self.logoutButton, 0)
        self.hBoxLayout.addSpacing(16)

    def _bind(self) -> None:
        self.qrButton.clicked.connect(self._onQrClicked)
        self.desktopButton.clicked.connect(self._onDesktopClicked)
        self.refreshButton.clicked.connect(self._onRefreshClicked)
        self.logoutButton.clicked.connect(self._onLogoutClicked)
        self._account.stateChanged.connect(self.refreshLoginInfo, owner=self)

    def refreshLoginInfo(self) -> None:
        status = self._account.statusText
        error = self._account.lastError
        content = f"{status}（{error}）" if error else status
        self.setContent(content)

        isInstalled = self._account.isInstalled
        isLoggingIn = self._account.isLoggingIn
        isBusy = self._account.isBusy

        self.qrButton.setEnabled(isInstalled and not isLoggingIn)
        self.desktopButton.setEnabled(isInstalled and not isBusy)
        self.refreshButton.setEnabled(not isLoggingIn)
        self.logoutButton.setEnabled(self._account.isLoggedIn and not isBusy)

    def _onQrClicked(self) -> None:
        dialog = TelegramScanLoginDialog(self._account, self.window())
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def _onDesktopClicked(self) -> None:
        dialog = TelegramDesktopLoginDialog(self.window())
        try:
            isConfirmed = dialog.exec()
            desktopPath = dialog.desktopPath
            passcode = dialog.passcode
        finally:
            dialog.deleteLater()
        if not isConfirmed:
            return
        self._account.startLogin("desktop", desktopPath=desktopPath, passcode=passcode)

    def _onRefreshClicked(self) -> None:
        self._account.refreshAuth()

    def _onLogoutClicked(self) -> None:
        dialog = MessageBox(
            self.tr("退出 Telegram 登录"),
            self.tr("确定要清除当前命名空间的 Telegram 登录会话吗？"),
            self.window(),
        )
        try:
            isConfirmed = dialog.exec()
        finally:
            dialog.deleteLater()
        if isConfirmed:
            self._account.logout()
