using System;
using System.IO;
using System.Linq;
using System.Net.Mime;
using System.Threading;
using System.Threading.Tasks;
using Jellyfin.Database.Implementations.Entities;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Trickplay;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Logging;
using SkiaSharp;

namespace Jellyfin.Plugin.MediaPreviewBridge.Api;

/// <summary>
/// Single REST endpoint that registers externally-published trickplay
/// tiles with Jellyfin's TrickplayInfos store. Saves the publisher
/// from having to flip <c>ExtractTrickplayImagesDuringLibraryScan</c>
/// (and racing concurrent library scans) just to make Jellyfin notice
/// tiles that are already on disk.
/// </summary>
[ApiController]
// Admin-only — string-literal because Jellyfin.Api.Constants.Policies
// isn't on the plugin's assembly classpath. The names are stable per
// release-10.11.z's Jellyfin.Server/Extensions/ApiServiceCollectionExtensions.cs.
[Authorize(Policy = "RequiresElevation")]
[Route("MediaPreviewBridge")]
[Produces(MediaTypeNames.Application.Json)]
public class TrickplayBridgeController : ControllerBase
{
    private readonly ILibraryManager _libraryManager;
    private readonly ITrickplayManager _trickplayManager;
    private readonly ILogger<TrickplayBridgeController> _logger;

    /// <summary>
    /// Initialises a new instance.
    /// </summary>
    /// <param name="libraryManager">Jellyfin's library manager (for item lookup).</param>
    /// <param name="trickplayManager">Jellyfin's trickplay manager (for SaveTrickplayInfo).</param>
    /// <param name="logger">DI logger.</param>
    public TrickplayBridgeController(
        ILibraryManager libraryManager,
        ITrickplayManager trickplayManager,
        ILogger<TrickplayBridgeController> logger)
    {
        _libraryManager = libraryManager;
        _trickplayManager = trickplayManager;
        _logger = logger;
    }

    /// <summary>
    /// Health-probe + plugin-detection endpoint. The Python publisher
    /// hits this on connection-test to know whether the plugin is
    /// installed; if it returns 200, skip the brief flag-flip fallback.
    /// </summary>
    /// <returns>200 with a tiny JSON body identifying the plugin.</returns>
    [HttpGet("Ping")]
    [AllowAnonymous]
    public IActionResult Ping()
    {
        return Ok(new
        {
            plugin = "MediaPreviewBridge",
            version = Plugin.Instance?.Version?.ToString() ?? "unknown",
            ok = true,
        });
    }

    /// <summary>
    /// Resolve an absolute file path to its Jellyfin item id.
    ///
    /// Wraps <c>ILibraryManager.FindByPath</c>, which is a single
    /// equality lookup against an indexed column on the BaseItems
    /// table — sub-millisecond on libraries of any size. Lets the
    /// publisher skip the public <c>/Items?searchTerm=…</c> API,
    /// whose full-text title index silently strips tokens like
    /// <c>4K</c>, <c>HDR</c>, <c>DV</c> and release-group brackets
    /// (so a filename like <c>Test (2024) [imdb-…][HDR10][x265]-NAHOM.mkv</c>
    /// returns no hits even though the item is indexed).
    /// </summary>
    /// <param name="path">Absolute file path as Jellyfin sees it (must match the indexed Path column exactly).</param>
    /// <returns>200 with <c>{itemId, name, type}</c> on a hit, 404 when no item has that path.</returns>
    [HttpGet("ResolvePath")]
    [ProducesResponseType(StatusCodes.Status200OK)]
    [ProducesResponseType(StatusCodes.Status400BadRequest)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    public IActionResult ResolvePath([FromQuery] string? path)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            return BadRequest(new { error = "path query parameter is required" });
        }

        var item = _libraryManager.FindByPath(path, isFolder: false);
        if (item is null)
        {
            return NotFound(new { error = $"no item with path {path}" });
        }

        return Ok(new
        {
            itemId = item.Id,
            name = item.Name,
            type = item.GetType().Name,
        });
    }

    /// <summary>
    /// Register the trickplay tiles a publisher just wrote next to a
    /// media file. Jellyfin scans the per-resolution sub-directory
    /// (<c>&lt;basename&gt;.trickplay/&lt;width&gt; - &lt;tileW&gt;x&lt;tileH&gt;</c>),
    /// computes <see cref="TrickplayInfo" /> from the on-disk files,
    /// and persists it via <c>ITrickplayManager.SaveTrickplayInfo</c>
    /// — no ffmpeg, no flag flip.
    /// </summary>
    /// <param name="itemId">Jellyfin item id (GUID with or without dashes).</param>
    /// <param name="width">Trickplay resolution width in pixels (default 320).</param>
    /// <param name="intervalMs">Frame interval in milliseconds (default 10000 = one frame every 10s).</param>
    /// <param name="cancellationToken">Cancellation token.</param>
    /// <returns>204 on success, 404 if the item or sheet directory is missing, 400 on bad input.</returns>
    [HttpPost("Trickplay/{itemId:guid}")]
    [ProducesResponseType(StatusCodes.Status204NoContent)]
    [ProducesResponseType(StatusCodes.Status400BadRequest)]
    [ProducesResponseType(StatusCodes.Status404NotFound)]
    public async Task<IActionResult> RegisterTrickplay(
        [FromRoute] Guid itemId,
        [FromQuery] int width = 320,
        [FromQuery] int intervalMs = 10000,
        CancellationToken cancellationToken = default)
    {
        if (width <= 0 || intervalMs <= 0)
        {
            return BadRequest(new { error = "width and intervalMs must be positive" });
        }

        var item = _libraryManager.GetItemById<BaseItem>(itemId);
        if (item is null)
        {
            return NotFound(new { error = $"item {itemId:D} not found" });
        }
        if (item is not Video)
        {
            return BadRequest(new { error = $"item {itemId:D} is not a Video ({item.GetType().Name})" });
        }

        // We always use Jellyfin's standard 10x10 tile layout — matches
        // what JellyfinTrickplayAdapter writes. Hard-coded here so the
        // wire format stays single-shape; extending later is easy.
        const int tileWidth = 10;
        const int tileHeight = 10;

        // Build the path the publisher must have written to. Mirrors
        // Jellyfin's PathManager.GetTrickplayDirectory(item, saveWithMedia=true)
        // + the per-resolution sub-dir formatted as "<width> - <tileW>x<tileH>".
        var containingFolder = item.ContainingFolderPath;
        var basename = Path.GetFileNameWithoutExtension(item.Path);
        var sheetsDir = Path.Combine(
            containingFolder,
            basename + ".trickplay",
            $"{width.ToString(System.Globalization.CultureInfo.InvariantCulture)} - " +
            $"{tileWidth.ToString(System.Globalization.CultureInfo.InvariantCulture)}x" +
            $"{tileHeight.ToString(System.Globalization.CultureInfo.InvariantCulture)}");

        if (!Directory.Exists(sheetsDir))
        {
            return NotFound(new
            {
                error = $"sheet directory does not exist: {sheetsDir}",
                hint = "Did the publisher write tiles before calling this endpoint?",
            });
        }

        var sheetFiles = Directory
            .EnumerateFiles(sheetsDir, "*.jpg")
            .Select(p =>
            {
                var name = Path.GetFileNameWithoutExtension(p);
                return int.TryParse(name, System.Globalization.NumberStyles.None, System.Globalization.CultureInfo.InvariantCulture, out var idx)
                    ? (Index: idx, Path: p)
                    : (Index: -1, Path: p);
            })
            .Where(t => t.Index >= 0)
            .OrderBy(t => t.Index)
            .ToList();

        if (sheetFiles.Count == 0)
        {
            return NotFound(new { error = $"no numeric .jpg tiles in {sheetsDir}" });
        }

        // Measure both the first sheet (for thumb dimensions) and the
        // last sheet (for partial-fill detection). Jellyfin's HLS
        // playlist generator uses ThumbnailCount as the *individual
        // thumbnail* count and divides by tileWidth*tileHeight to
        // figure out how many sheets to reference — so we MUST count
        // real thumbnails, not sheet files. (Jellyfin's own
        // import-existing branch sets ThumbnailCount = existingFiles.Length
        // which is the SHEET count and produces a broken playlist —
        // see Jellyfin issue #12887. Don't follow that path.)
        var firstSheetPath = sheetFiles[0].Path;
        var lastSheetPath = sheetFiles[^1].Path;
        int thumbWidth, thumbHeight, lastSheetFilled;
        try
        {
            using var firstSheet = SKBitmap.Decode(firstSheetPath);
            if (firstSheet is null)
            {
                return BadRequest(new { error = $"could not decode first sheet {firstSheetPath}" });
            }
            thumbWidth = firstSheet.Width / tileWidth;
            thumbHeight = firstSheet.Height / tileHeight;

            using var lastSheet = SKBitmap.Decode(lastSheetPath);
            if (lastSheet is null)
            {
                return BadRequest(new { error = $"could not decode last sheet {lastSheetPath}" });
            }
            lastSheetFilled = sheetFiles.Count == 1
                ? CountFilledTiles(lastSheet, tileWidth, tileHeight, thumbWidth, thumbHeight)
                : CountFilledTiles(lastSheet, tileWidth, tileHeight, thumbWidth, thumbHeight);
        }
        catch (Exception exc)
        {
            return BadRequest(new { error = $"could not decode sheet: {exc.Message}" });
        }

        if (thumbWidth <= 0 || thumbHeight <= 0)
        {
            return BadRequest(new { error = $"invalid thumb size {thumbWidth}x{thumbHeight} from {firstSheetPath}" });
        }

        var tilesPerSheet = tileWidth * tileHeight;
        // Total individual thumbnails = (full-sheets * 100) + filled-tiles in last sheet.
        // If the last sheet looks empty (CountFilledTiles returned 0)
        // assume it's at least 1, since the file exists for a reason.
        var thumbnailCount = ((sheetFiles.Count - 1) * tilesPerSheet) + Math.Max(1, lastSheetFilled);

        // Bandwidth is in bits/sec — Jellyfin's HLS playlist surfaces it
        // as max-bitrate metadata. Compute per-tile size from the sheet
        // average so the value scales with the actual data we wrote.
        long totalBytes = sheetFiles.Sum(t => new FileInfo(t.Path).Length);
        var avgTileBytes = Math.Max(1, totalBytes / Math.Max(1, thumbnailCount));
        var bandwidth = (int)(avgTileBytes * 8 * 1000 / intervalMs);

        var info = new TrickplayInfo
        {
            ItemId = item.Id,
            Width = width,
            Height = thumbHeight,
            TileWidth = tileWidth,
            TileHeight = tileHeight,
            ThumbnailCount = thumbnailCount,
            Interval = intervalMs,
            Bandwidth = bandwidth,
        };

        await _trickplayManager.SaveTrickplayInfo(info).ConfigureAwait(false);

        _logger.LogInformation(
            "MediaPreviewBridge: registered trickplay for {ItemId} " +
            "({Sheets} sheets, {Thumbs} thumbs, {Width}x{Height} px tiles, " +
            "{Interval}ms interval, {Bandwidth} bits/s)",
            item.Id,
            sheetFiles.Count,
            thumbnailCount,
            width,
            thumbHeight,
            intervalMs,
            bandwidth);

        return NoContent();
    }

    /// <summary>
    /// Count tiles in a sheet that aren't fully black. Walk from the
    /// last position backwards; the first non-black tile + 1 is the
    /// filled count. Lets us register the right thumbnail count when
    /// the publisher wrote a partial last sheet.
    /// </summary>
    private static int CountFilledTiles(SKBitmap sheet, int tileWidth, int tileHeight, int thumbWidth, int thumbHeight)
    {
        var totalSlots = tileWidth * tileHeight;
        for (var slot = totalSlots - 1; slot >= 0; slot--)
        {
            var col = slot % tileWidth;
            var row = slot / tileWidth;
            // Probe a single pixel near the centre of the tile — far
            // cheaper than scanning the whole tile, and an empty slot
            // is uniformly black (the bg colour we paint into the
            // sheet image), so any non-black centre means the tile
            // was filled.
            var px = sheet.GetPixel(col * thumbWidth + thumbWidth / 2, row * thumbHeight + thumbHeight / 2);
            if (px.Red > 4 || px.Green > 4 || px.Blue > 4)
            {
                return slot + 1;
            }
        }
        return 0;
    }
}
