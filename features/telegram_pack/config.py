from __future__ import annotations

from typing import TYPE_CHECKING

from app.config.cfg import BoolValidator, ConfigItem, RangeConfigItem, RangeValidator
from app.config.paths import APP_DATA_DIR
from app.config.serializers import PathSerializer
from app.models.pack import PackConfig

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget
    from app.view.components.setting_card_group import CollapsibleSettingCardGroup
    from .account import TelegramAccount


class TelegramConfig(PackConfig):
    _account: TelegramAccount | None = None
    installFolder = ConfigItem(
        "Telegram", "InstallFolder", f"{APP_DATA_DIR}/TDL", serializer=PathSerializer(APP_DATA_DIR)
    )
    namespace = ConfigItem("Telegram", "Namespace", "default")
    threads = RangeConfigItem("Telegram", "Threads", 4, RangeValidator(1, 32))
    poolSize = RangeConfigItem("Telegram", "PoolSize", 8, RangeValidator(0, 32))
    proxyOverride = ConfigItem("Telegram", "ProxyOverride", "")
    inboxChatId = ConfigItem("Telegram", "InboxChatId", "")
    inboxChatTitle = ConfigItem("Telegram", "InboxChatTitle", "")
    useTakeout = ConfigItem("Telegram", "UseTakeout", False, BoolValidator())

    def settingGroups(self, parent: QWidget) -> list[CollapsibleSettingCardGroup]:
        from qfluentwidgets import FluentIcon, RangeSettingCard, SwitchSettingCard
        from app.view.components.setting_card_group import CollapsibleSettingCardGroup
        from app.view.components.setting_cards import LineEditSettingCard, SelectFolderSettingCard
        from .runtime import tdlRuntime
        from .setting_cards import TelegramLoginSettingCard

        group = CollapsibleSettingCardGroup(self.tr("Telegram"), "telegram", parent)
        installFolderCard = SelectFolderSettingCard(
            self.installFolder,
            f"{APP_DATA_DIR}/TDL",
            self.tr("tdl 安装目录"),
            group,
        )
        cards = [installFolderCard]

        runtimeCard = None
        if self.createRuntimeCard is not None:
            runtimeCard = self.createRuntimeCard(tdlRuntime, group)
            cards.append(runtimeCard)

        if self._account is not None:
            cards.append(TelegramLoginSettingCard(self._account, group))

        cards.extend([
            RangeSettingCard(
                self.threads,
                FluentIcon.CLOUD,
                self.tr("单文件下载线程数"),
                self.tr("传给 tdl 的单文件并发线程数，独立于全局预分片数"),
                group,
            ),
            RangeSettingCard(
                self.poolSize,
                FluentIcon.SPEED_HIGH,
                self.tr("DC 连接池大小"),
                self.tr("Telegram 数据中心连接池大小，0 表示不限制"),
                group,
            ),
            SwitchSettingCard(
                FluentIcon.SYNC,
                self.tr("启用 Takeout 会话"),
                self.tr("批量导出或下载大量文件时可降低限流风险"),
                self.useTakeout,
                group,
            ),
            LineEditSettingCard(
                FluentIcon.TAG,
                self.tr("会话命名空间"),
                self.tr("tdl 使用的账号命名空间，默认为 default"),
                self.namespace,
                group,
                placeholder="default",
            ),
            LineEditSettingCard(
                FluentIcon.GLOBE,
                self.tr("独立代理覆盖"),
                self.tr("留空时自动继承全局代理；支持 socks5:// 或 http://"),
                self.proxyOverride,
                group,
                placeholder="socks5://127.0.0.1:7890",
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
                    if status.runtimeId == tdlRuntime.runtimeId and not status.isInstalling and not status.isBusy:
                        account.refreshAuth()

                statusService.statusChanged.connect(onRuntimeStatusChanged, owner=runtimeCard)
        return [group]


telegramConfig = TelegramConfig()
