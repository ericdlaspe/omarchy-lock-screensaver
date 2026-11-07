QT += quick qml testlib
CONFIG += testcase console c++17
CONFIG -= app_bundle
TARGET = renderer-test
SOURCES += $$PWD/renderer_test.cpp $$PWD/../../native/renderer.cpp
HEADERS += $$PWD/../../native/renderer.h
