from __future__ import annotations

from typing import TYPE_CHECKING

from app.config.cfg import (
    ConfigItem,
    OptionsConfigItem,
    OptionsValidator,
    RangeConfigItem,
    RangeValidator,
)
from app.config.paths import APP_DATA_DIR
from app.config.serializers import PathSerializer
from app.models.pack import PackConfig

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget
    from app.view.components.setting_card_group import CollapsibleSettingCardGroup
    from .account import BaiduAccount

DOWNLOAD_MODES = ("locate", "pcs", "stream")


class BaiduConfig(PackConfig):
    _account: BaiduAccount | None = None
    installFolder = ConfigItem(
        "Baidu", "InstallFolder", f"{APP_DATA_DIR}/BaiduPCS", serializer=PathSerializer(APP_DATA_DIR),
    )
    downloadMode = OptionsConfigItem(
        "Baidu", "DownloadMode", "locate", OptionsValidator(list(DOWNLOAD_MODES)),
    )
    threads = RangeConfigItem("Baidu", "Threads", 8, RangeValidator(1, 64))

    def settingGroups(self, parent: QWidget) -> list[CollapsibleSettingCardGroup]:
        from qfluentwidgets import ComboBoxSettingCard, FluentIcon, RangeSettingCard
        from app.view.components.setting_card_group import CollapsibleSettingCardGroup
        from app.view.components.setting_cards import SelectFolderSettingCard
        from .runtime import baiduPcsRuntime
        from .setting_cards import BaiduLoginSettingCard

        group = CollapsibleSettingCardGroup(self.tr("百度网盘"), "baidu", parent)
        installFolderCard = SelectFolderSettingCard(
            self.installFolder,
            f"{APP_DATA_DIR}/BaiduPCS",
            self.tr("BaiduPCS-Go 安装目录"),
            group,
        )
        cards = [installFolderCard]

        runtimeCard = None
        if self.createRuntimeCard is not None:
            runtimeCard = self.createRuntimeCard(baiduPcsRuntime, group)
            cards.append(runtimeCard)

        if self._account is not None:
            cards.append(BaiduLoginSettingCard(self._account, group))

        cards.extend([
            ComboBoxSettingCard(
                self.downloadMode,
                FluentIcon.CLOUD_DOWNLOAD,
                self.tr("下载模式"),
                self.tr("传给 BaiduPCS-Go 的下载模式，locate 不可用时可切换 pcs 或 stream"),
                texts=list(DOWNLOAD_MODES),
                parent=group,
            ),
            RangeSettingCard(
                self.threads,
                FluentIcon.CLOUD,
                self.tr("单文件并发线程数"),
                self.tr("传给 BaiduPCS-Go 的单文件下载并发线程数（-p）"),
                group,
            ),
        ])

        group.addSettingCards(cards)
        if runtimeCard is not None:
            installFolderCard.pathChanged.connect(runtimeCard._onInstallFolderChanged)
            runtimeCard.refreshStatus()
        if self._account is not None:
            account = self._account
            installFolderCard.pathChanged.connect(lambda *_: account.refreshAuth())
            statusService = getattr(runtimeCard, "_runtimeStatusService", None) if runtimeCard is not None else None
            if statusService is not None and hasattr(statusService, "statusChanged"):
                def onRuntimeStatusChanged(status) -> None:
                    if status.runtimeId == baiduPcsRuntime.runtimeId and not status.isInstalling and not status.isBusy:
                        account.refreshAuth()

                statusService.statusChanged.connect(onRuntimeStatusChanged, owner=runtimeCard)
        return [group]


baiduConfig = BaiduConfig()
