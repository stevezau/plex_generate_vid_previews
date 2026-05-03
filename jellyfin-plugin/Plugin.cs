using System;
using System.Collections.Generic;
using Jellyfin.Plugin.MediaPreviewBridge.Configuration;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;

namespace Jellyfin.Plugin.MediaPreviewBridge;

/// <summary>
/// Bridge plugin that lets an external trickplay generator (the Media
/// Preview Generator app) tell Jellyfin "I just wrote tiles to disk for
/// item X — please register them" without going through Jellyfin's own
/// ffmpeg pipeline. Exposes a single REST endpoint
/// (<see cref="Api.TrickplayBridgeController" />) that internally calls
/// <c>ITrickplayManager.SaveTrickplayInfo</c>.
///
/// Why this exists: Jellyfin's only public path for trickplay
/// registration is <c>RefreshTrickplayDataAsync</c>, gated by
/// <c>ExtractTrickplayImagesDuringLibraryScan</c>. With that flag off,
/// externally-published trickplay sits on disk forever invisible to the
/// player. This plugin closes that gap with a single internal API call.
/// </summary>
public class Plugin : BasePlugin<PluginConfiguration>, IHasWebPages
{
    /// <summary>
    /// Initialises the singleton and lets <see cref="BasePlugin{T}" />
    /// load <see cref="PluginConfiguration" /> from disk.
    /// </summary>
    /// <param name="applicationPaths">Standard Jellyfin paths (config root etc.).</param>
    /// <param name="xmlSerializer">Jellyfin-managed XML serialiser used for plugin config persistence.</param>
    public Plugin(IApplicationPaths applicationPaths, IXmlSerializer xmlSerializer)
        : base(applicationPaths, xmlSerializer)
    {
        Instance = this;
    }

    /// <summary>
    /// Gets the singleton instance — the plugin doesn't itself need
    /// global state, but Jellyfin's plugin loader expects one.
    /// </summary>
    public static Plugin? Instance { get; private set; }

    /// <inheritdoc />
    public override string Name => "Media Preview Bridge";

    /// <inheritdoc />
    public override Guid Id => Guid.Parse("c2cb9bf9-7c5d-4f1a-9a07-2d6f5e5b0001");

    /// <inheritdoc />
    public override string Description =>
        "Lets external preview-generator tools (e.g. Media Preview Generator) " +
        "register pre-written trickplay tiles with Jellyfin without spawning " +
        "ffmpeg. POST /MediaPreviewBridge/Trickplay/{itemId} after writing " +
        "tiles to <basename>.trickplay/<width> - <tileW>x<tileH>/<n>.jpg.";

    /// <inheritdoc />
    public IEnumerable<PluginPageInfo> GetPages()
    {
        // No HTML config page right now — the plugin is wire-only. If we
        // add one later it lives at Configuration/configPage.html.
        return Array.Empty<PluginPageInfo>();
    }
}
