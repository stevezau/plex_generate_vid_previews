using MediaBrowser.Model.Plugins;

namespace Jellyfin.Plugin.MediaPreviewBridge.Configuration;

/// <summary>
/// Plugin configuration. Currently has no user-facing knobs — the plugin
/// is a pure passthrough (registers the trickplay info that an external
/// publisher already wrote to disk). Kept as a placeholder so the
/// Jellyfin admin's Plugins → Media Preview Bridge page renders, even
/// if it just shows version + help text.
/// </summary>
public class PluginConfiguration : BasePluginConfiguration
{
}
