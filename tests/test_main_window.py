from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import qfluentwidgets
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.models.pack import PackServices
from app.services.plan import Plan
from app.view.qfw_patch import patchLinuxFrameless
from app.view.windows.main_window import MainWindow
from quark_pack.config import quarkConfig
from quark_pack.pack import QuarkPack
from quark_pack.page import QuarkPage
from telegram_pack.config import telegramConfig
from telegram_pack.pack import TelegramPack
from telegram_pack.page import TelegramPage


@pytest.fixture
def mainWindow(qapp, engine, monkeypatch):
    qfluentwidgets.load(qapp)
    if sys.platform == "linux":
        patchLinuxFrameless()
    # 屏蔽 offscreen 平台下依赖真实原生窗口句柄的系统外观钩子
    monkeypatch.setattr(MainWindow, "updateFrameless", lambda self: self.winId())
    monkeypatch.setattr(MainWindow, "setMicaEffectEnabled", lambda self, _enabled: None)
    monkeypatch.setattr(MainWindow, "_refreshBackgroundEffect", lambda self: None)
    monkeypatch.setattr(MainWindow, "_onSpeedChanged", lambda self, _speed: None)

    quarkConfig.cookie.value = ""
    telegramConfig.inboxChatId.value = ""
    services = PackServices(
        coroutineRunner=engine.coroutineRunner,
        speedMeter=engine.speedMeter,
    )
    engine.featureService.register(QuarkPack(services))
    engine.featureService.register(TelegramPack(services))

    window = MainWindow(
        engine.taskService,
        engine.featureService,
        engine.browserService,
        engine.browserListener,
        engine.aria2RpcListener,
        engine.categoryService,
        engine.speedMeter,
        engine.coroutineRunner,
        Plan(allCompleted=lambda: True),
    )
    window.setupPacks()

    yield window

    window.close()
    if sys.platform == "darwin":
        window.deleteLater()
    QApplication.processEvents()


async def test_click_pack_navigation_opens_quark_and_telegram_pages(mainWindow, qtbot):
    qtbot.mouseClick(mainWindow.navigationInterface.items["QuarkPage"], Qt.MouseButton.LeftButton)
    quarkPage = mainWindow.stackedWidget.currentWidget()
    assert isinstance(quarkPage, QuarkPage)
    assert quarkPage.objectName() == "QuarkPage"
    assert mainWindow.searchEdit.placeholderText() == QuarkPage.searchPlaceholder

    qtbot.mouseClick(mainWindow.navigationInterface.items["TelegramPage"], Qt.MouseButton.LeftButton)
    telegramPage = mainWindow.stackedWidget.currentWidget()
    assert isinstance(telegramPage, TelegramPage)
    assert telegramPage.objectName() == "TelegramPage"
    assert mainWindow.searchEdit.placeholderText() == TelegramPage.searchPlaceholder


async def test_click_pack_navigation_reuses_created_page(mainWindow, qtbot):
    quarkNav = mainWindow.navigationInterface.items["QuarkPage"]
    taskNav = mainWindow.navigationInterface.items["TaskPage"]

    qtbot.mouseClick(quarkNav, Qt.MouseButton.LeftButton)
    firstPage = mainWindow.stackedWidget.currentWidget()
    assert isinstance(firstPage, QuarkPage)

    qtbot.mouseClick(taskNav, Qt.MouseButton.LeftButton)
    assert mainWindow.stackedWidget.currentWidget().objectName() == "TaskPage"

    qtbot.mouseClick(quarkNav, Qt.MouseButton.LeftButton)
    assert mainWindow.stackedWidget.currentWidget() is firstPage
