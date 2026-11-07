QT += quick qml
TEMPLATE = lib
CONFIG += plugin c++17
TARGET = locksaver
DESTDIR = $$OUT_PWD/module
SOURCES += $$PWD/renderer.cpp $$PWD/plugin.cpp
HEADERS += $$PWD/renderer.h
