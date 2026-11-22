from __future__ import annotations

from typing import TYPE_CHECKING

from app.config.cfg import BoolValidator, ConfigItem, RangeConfigItem, RangeValidator
from app.models.pack import PackConfig

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget
    from app.view.components.setting_card_group import CollapsibleSettingCardGroup
    from .account import QuarkAccount


class QuarkConfig(PackConfig):
    _account: QuarkAccount
    cookie = ConfigItem("Quark", "Cookie", "")
    subworkerCount = RangeConfigItem("Quark", "SubworkerCount", 8, RangeValidator(1, 32))
    shouldCleanTempAfterDone = ConfigItem("Quark", "CleanTempAfterDone", True, BoolValidator())

    def settingGroups(self, parent: QWidget) -> list[CollapsibleSettingCardGroup]:
        from qfluentwidgets import FluentIcon, RangeSettingCard, SwitchSettingCard
        from app.view.components.setting_card_group import CollapsibleSettingCardGroup
        from .setting_cards import QuarkLoginSettingCard

        group = CollapsibleSettingCardGroup(self.tr("夸克网盘"), "quark", parent)
        group.addSettingCards([
            QuarkLoginSettingCard(self._account, group),
            RangeSettingCard(self.subworkerCount, FluentIcon.CLOUD, self.tr("分片并发"),
                             self.tr("夸克下载的分片数，独立于全局预分片数"), group),
            SwitchSettingCard(FluentIcon.DELETE, self.tr("完成后清理临时目录"),
                              self.tr("分享下载完成后删除本次网盘临时目录"),
                              self.shouldCleanTempAfterDone, group),
        ])
        return [group]


quarkConfig = QuarkConfig()
