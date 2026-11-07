#pragma once
#include <QtQuick/QQuickPaintedItem>
#include <QFont>
#include <QVariantList>
#include <atomic>

// Runs are [zero-based row, column, #RRGGBB, plain text]. ASCII runs may
// contain multiple cells; the bridge emits each Unicode grapheme separately
// at its absolute terminal-cell coordinate (including wide-cell continuations).
class TerminalFrame : public QQuickPaintedItem {
    Q_OBJECT
    Q_PROPERTY(QVariantList runs READ runs WRITE setRuns NOTIFY runsChanged)
    Q_PROPERTY(int cellFontSize READ cellFontSize WRITE setCellFontSize NOTIFY cellFontSizeChanged)
    Q_PROPERTY(quint64 paintCount READ paintCount)
    Q_PROPERTY(qreal cellWidth READ cellWidth NOTIFY cellFontSizeChanged)
    Q_PROPERTY(qreal cellHeight READ cellHeight NOTIFY cellFontSizeChanged)
public:
    explicit TerminalFrame(QQuickItem *parent = nullptr);
    QVariantList runs() const { return m_runs; }
    void setRuns(const QVariantList &runs);
    int cellFontSize() const { return m_fontSize; }
    void setCellFontSize(int value);
    quint64 paintCount() const { return m_paintCount.load(std::memory_order_relaxed); }
    qreal cellWidth() const { return m_cellWidth; }
    qreal cellHeight() const { return m_lineHeight; }
    void paint(QPainter *painter) override;
signals:
    void runsChanged();
    void cellFontSizeChanged();
private:
    struct Run { int row; int column; QColor color; QString text; };
    void updateFont();
    QVariantList m_runs;
    QList<Run> m_parsed;
    int m_fontSize = 20;
    std::atomic<quint64> m_paintCount{0};
    QFont m_font;
    qreal m_cellWidth = 12;
    qreal m_lineHeight = 24;
    qreal m_ascent = 20;
};
