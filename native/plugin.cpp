#include <QtQml/QQmlExtensionPlugin>
#include <QtQml/qqml.h>
#include "renderer.h"

class LockSaverPlugin : public QQmlExtensionPlugin {
    Q_OBJECT
    Q_PLUGIN_METADATA(IID QQmlExtensionInterface_iid)
public:
    void registerTypes(const char *uri) override {
        qmlRegisterType<TerminalFrame>(uri, 1, 0, "TerminalFrame");
    }
};
#include "plugin.moc"
