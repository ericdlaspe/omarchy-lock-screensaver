#include <QtTest>
#include <QGuiApplication>
#include <QFontDatabase>
#include <QFontMetricsF>
#include <QImage>
#include <QPainter>
#include <QQmlEngine>
#include <QQmlComponent>
#include <QQuickWindow>
#include <QThread>
#include <QTimer>
#include <QDebug>
#include <atomic>
#include <thread>
#include "../../native/renderer.h"

class RendererTest : public QObject {
    Q_OBJECT
private slots:
    void validatesRuns() {
        TerminalFrame item;
        item.setRuns({QVariantList{1, 2, "#12ab34", "ok"},
                      QVariantList{-1, 0, "#ffffff", "bad"},
                      QVariantList{120, 0, "#ffffff", "bad"},
                      QVariantList{0, 300, "#ffffff", "bad"},
                      QVariantList{0.5, 0, "#ffffff", "bad"},
                      QVariantList{"zero", 0, "#ffffff", "bad"},
                      QVariantList{true, 0, "#ffffff", "bad"},
                      QVariantList{0, 0, "not-color", "bad"},
                      QVariantList{0, 0, "#ffffff", "new\nline"},
                      QVariantList{0, 0, "#ffffff", 123},
                      QVariantList{0, 0, "#ffffff"}});
        QCOMPARE(item.runs().size(), 1);
        item.setRuns({QVariant(QVariantList{119, 299, "#123456", "AB"})});
        QCOMPARE(item.runs().size(), 1);
        QCOMPARE(item.runs()[0].toList()[3].toString(), QString("A"));
        QVariantList tooMany;
        for (int i = 0; i < 36001; ++i) tooMany.append(QVariantList{0, 0, "#ffffff", "x"});
        item.setRuns(tooMany);
        QVERIFY(item.runs().isEmpty());
    }

    void paintsAtAbsoluteCellPositions() {
        TerminalFrame item;
        item.setCellFontSize(20);
        item.setRuns({QVariant(QVariantList{2, 4, "#ffffff", "MM"})});
        QImage image(400, 160, QImage::Format_ARGB32_Premultiplied);
        image.fill(Qt::black);
        QPainter painter(&image);
        item.paint(&painter);
        painter.end();
        int minX = image.width(), minY = image.height(), maxX = -1, maxY = -1;
        for (int y = 0; y < image.height(); ++y)
            for (int x = 0; x < image.width(); ++x)
                if (image.pixelColor(x, y) != QColor(Qt::black)) {
                    minX = qMin(minX, x); maxX = qMax(maxX, x);
                    minY = qMin(minY, y); maxY = qMax(maxY, y);
                }
        QVERIFY(minX >= qFloor(2 + 4 * item.cellWidth()));
        QVERIFY(maxX < qCeil(2 + 6 * item.cellWidth()) + 1);
        QVERIFY(minY >= qFloor(2 + 2 * item.cellHeight()));
        QVERIFY(maxY < qCeil(2 + 3 * item.cellHeight()) + 1);
        QCOMPARE(item.paintCount(), quint64(1));
        item.setRuns({QVariantList{0, 0, "#ffffff", "<svg>"},
                      QVariantList{1, 1, "#ff0000", QString::fromUtf8("界")}});
        QCOMPARE(item.runs().size(), 2); // text, never markup
    }

    void combiningGraphemeStaysInOneCell() {
        TerminalFrame item;
        const QString text = QStringLiteral("x\u0301\u0308");
        item.setRuns({QVariant(QVariantList{0, 0, "#ffffff", text})});
        QCOMPARE(item.runs().size(), 1);
        QImage actual(100, 100, QImage::Format_ARGB32_Premultiplied);
        actual.fill(Qt::black);
        QPainter painter(&actual);
        item.paint(&painter);
        painter.end();
        QImage expected(actual.size(), actual.format());
        expected.fill(Qt::black);
        QPainter reference(&expected);
        QFont font = QFontDatabase::systemFont(QFontDatabase::FixedFont);
        font.setStyleHint(QFont::TypeWriter);
        font.setKerning(false);
        font.setPixelSize(20);
        reference.setFont(font);
        reference.setPen(Qt::white);
        reference.setClipRect(QRectF(2, 2, 300 * item.cellWidth(), 120 * item.cellHeight()));
        reference.drawText(QPointF(2, 2 + QFontMetricsF(font).ascent()), text);
        reference.end();
        QCOMPARE(actual, expected);
    }

    void atomicActualPaintCounter() {
        TerminalFrame item;
        QCOMPARE(item.paintCount(), quint64(0));
        item.setRuns({QVariant(QVariantList{0, 0, "#ffffff", "x"})});
        QCOMPARE(item.paintCount(), quint64(0)); // a submitted frame is not a paint
        std::atomic<bool> done{false};
        std::thread reader([&] { while (!done.load()) (void)item.paintCount(); });
        QImage image(100, 100, QImage::Format_ARGB32_Premultiplied);
        QPainter painter(&image);
        for (int i = 0; i < 100; ++i) item.paint(&painter);
        done.store(true);
        reader.join();
        QCOMPARE(item.paintCount(), quint64(100));
    }

    void pluginLoadsAndSceneGraphPaints() {
        QQmlEngine engine;
        QQmlComponent component(&engine);
        const QByteArray module = qgetenv("LOCKSAVER_MODULE_URL");
        QVERIFY2(!module.isEmpty(), "Set LOCKSAVER_MODULE_URL to built module file URL");
        component.setData("import QtQuick\nimport \"" + module +
                          "\" as Native\nNative.TerminalFrame { width: 640; height: 480; "
                          "runs: [[0,0,\"#ffffff\",\"LIVE\"]] }", QUrl());
        QScopedPointer<QObject> object(component.create());
        QVERIFY2(object, qPrintable(component.errorString()));
        auto *item = qobject_cast<QQuickItem *>(object.data());
        QVERIFY(item);
        QCOMPARE(object->property("runs").toList().size(), 1);
        QCOMPARE(object->property("runs").toList()[0].toList()[3].toString(), QString("LIVE"));
        QQuickWindow window;
        window.resize(640, 480);
        item->setParentItem(window.contentItem());
        window.show();
        QTRY_VERIFY_WITH_TIMEOUT(object->property("paintCount").toULongLong() > 0, 5000);
        qInfo() << "actual scene-graph paintCount:" << object->property("paintCount");
        item->setParentItem(nullptr);
    }
};

int main(int argc, char **argv) {
    QGuiApplication app(argc, argv);
    // Execute the exact generated prompt fragment without Quickshell or a
    // compositor, so the functional timer checks also run in Arch CI.
    if (argc == 3 && QByteArray(argv[1]) == "--qml-test") {
        QQmlEngine engine;
        bool failed = false;
        QObject::connect(&engine, &QQmlEngine::warnings, &app, [&](const QList<QQmlError> &errors) {
            failed = true;
            for (const auto &error : errors) qWarning() << error.toString();
            app.exit(1);
        });
        QObject::connect(&engine, &QQmlEngine::quit, &app, [&]() { app.exit(failed ? 1 : 0); });
        QQmlComponent component(&engine, QUrl::fromLocalFile(QString::fromLocal8Bit(argv[2])));
        QScopedPointer<QObject> object(component.create());
        if (!object || failed) { qWarning() << component.errorString(); return 1; }
        QTimer::singleShot(8000, &app, [&]() { app.exit(2); });
        return app.exec();
    }
    RendererTest test;
    return QTest::qExec(&test, argc, argv);
}
#include "renderer_test.moc"
