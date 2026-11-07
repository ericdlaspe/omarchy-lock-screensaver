#include "renderer.h"
#include <QFontDatabase>
#include <QFontMetricsF>
#include <QPainter>
#include <QTextBoundaryFinder>
#include <algorithm>
#include <cmath>

namespace {
bool gridIndex(const QVariant &value, int limit, int &result) {
    const int type = value.metaType().id();
    if (type != QMetaType::Int && type != QMetaType::UInt &&
        type != QMetaType::LongLong && type != QMetaType::ULongLong &&
        type != QMetaType::Double && type != QMetaType::Float) return false;
    bool ok = false;
    const double number = value.toDouble(&ok);
    if (!ok || !std::isfinite(number) || number < 0 || number >= limit ||
        number != std::floor(number)) return false;
    result = static_cast<int>(number);
    return true;
}

bool plainText(QString &text, int cells) {
    if (text.isEmpty() || text.size() > 1200) return false;
    bool ascii = true;
    for (const QChar c : text) {
        if (c.category() == QChar::Other_Control || c.isNull()) return false;
        if (c.unicode() >= 128) ascii = false;
    }
    if (ascii) {
        text.truncate(cells);
        return true;
    }
    // Only one Unicode terminal cell per run. This avoids inferring terminal
    // width from a fallback font's advance and preserves later cell positions.
    if (text.size() > 32) return false;
    QTextBoundaryFinder finder(QTextBoundaryFinder::Grapheme, text);
    return finder.toNextBoundary() == text.size();
}
}

TerminalFrame::TerminalFrame(QQuickItem *parent) : QQuickPaintedItem(parent) {
    setAntialiasing(false);
    setFillColor(Qt::black);
    setOpaquePainting(true);
    updateFont();
}

void TerminalFrame::setRuns(const QVariantList &runs) {
    m_runs.clear();
    m_parsed.clear();
    if (runs.size() <= 36000) {
        m_runs.reserve(runs.size());
        m_parsed.reserve(runs.size());
        for (const auto &entry : runs) {
            if (entry.metaType().id() != QMetaType::QVariantList) continue;
            QVariantList run = entry.toList();
            int row = 0, col = 0;
            if (run.size() != 4 || !gridIndex(run[0], 120, row) ||
                !gridIndex(run[1], 300, col) ||
                run[2].metaType().id() != QMetaType::QString ||
                run[3].metaType().id() != QMetaType::QString) continue;
            const QString ink = run[2].toString();
            if (ink.size() != 7 || !ink.startsWith('#')) continue;
            bool hex = true;
            for (const QChar c : QStringView(ink).mid(1))
                if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
                      (c >= 'A' && c <= 'F'))) hex = false;
            if (!hex) continue;
            QString text = run[3].toString();
            if (!plainText(text, 300 - col)) continue;
            run[3] = text;
            m_runs.append(QVariant(run));
            m_parsed.append({row, col, QColor(ink), text});
        }
    }
    emit runsChanged();
    update();
}

void TerminalFrame::setCellFontSize(int value) {
    value = qBound(8, value, 40);
    if (m_fontSize == value) return;
    m_fontSize = value;
    updateFont();
    emit cellFontSizeChanged();
    update();
}

void TerminalFrame::updateFont() {
    m_font = QFontDatabase::systemFont(QFontDatabase::FixedFont);
    m_font.setStyleHint(QFont::TypeWriter);
    m_font.setKerning(false);
    m_font.setPixelSize(m_fontSize);
    const QFontMetricsF metrics(m_font);
    m_cellWidth = metrics.horizontalAdvance(QStringLiteral("M"));
    m_lineHeight = m_fontSize * 1.2;
    m_ascent = metrics.ascent();
}

void TerminalFrame::paint(QPainter *painter) {
    painter->save();
    painter->setFont(m_font);
    painter->setClipRect(QRectF(2, 2, 300 * m_cellWidth, 120 * m_lineHeight), Qt::IntersectClip);
    for (const auto &run : m_parsed) {
        painter->setPen(run.color);
        const qreal x = 2 + run.column * m_cellWidth;
        const qreal y = 2 + run.row * m_lineHeight + m_ascent;
        const bool ascii = std::all_of(run.text.cbegin(), run.text.cend(),
                                       [](QChar c) { return c.unicode() < 128; });
        if (!ascii || run.text.size() == 1) {
            painter->drawText(QPointF(x, y), run.text);
        } else {
            // ASCII glyphs also use explicit cells: kerning/ligatures/font
            // fallback must never drift relative to the terminal grid.
            for (int i = 0; i < run.text.size(); ++i)
                painter->drawText(QPointF(x + i * m_cellWidth, y), run.text.mid(i, 1));
        }
    }
    painter->restore();
    m_paintCount.fetch_add(1, std::memory_order_relaxed);
}
