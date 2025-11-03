"""
Command-line interface for Plex Video Preview Generator.

Main entry point that orchestrates all components: configuration,
GPU detection, Plex connection, and worker pool management.
"""

import os
import sys
import shutil
import signal
import argparse
from loguru import logger
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, MofNCompleteColumn, ProgressColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.live import Live
from rich.console import Group
from rich.text import Text

from .config import Config, load_config
from .gpu_detection import detect_all_gpus, format_gpu_info
from .plex_client import plex_server, get_library_sections
from .worker import WorkerPool
from .utils import calculate_title_width, setup_working_directory as create_working_directory, is_windows
from .version_check import check_for_updates
from .logging_config import setup_logging

# Shared console for coordinated logging and progress output
console = Console()


class ApplicationState:
    """Global application state for signal handling and cleanup."""
    def __init__(self):
        self.config = None
        self.console = console
    
    def set_config(self, config):
        """Set the configuration object."""
        self.config = config
    
    def cleanup(self):
        """Perform cleanup operations."""
        # Restore terminal cursor visibility using Rich's proper methods
        if self.console:
            try:
                # Rich's proper way to restore terminal state
                self.console.show_cursor(True)
                # Force Rich to restore the terminal to its original state
                if hasattr(self.console, '_live'):
                    self.console._live = None
                # Clear any pending output and ensure proper terminal state
                self.console.print("", end="")
                # Force a newline to ensure we're on a fresh line
                self.console.print()
            except Exception as e:
                # Fallback: direct terminal escape sequence
                try:
                    print('\033[?25h', end='', flush=True)
                    print()  # Ensure we're on a new line
                except:
                    pass
        
        # Clean up working tmp folder if it exists
        try:
            if self.config and hasattr(self.config, 'working_tmp_folder'):
                if os.path.isdir(self.config.working_tmp_folder):
                    shutil.rmtree(self.config.working_tmp_folder)
                    logger.debug(f"Cleaned up working temp folder: {self.config.working_tmp_folder}")
        except Exception as cleanup_error:
            logger.warning(f"Failed to clean up working temp folder during interrupt: {cleanup_error}")


# Global application state
app_state = ApplicationState()


class AnimatedBarColumn(BarColumn):
    """Custom animated progress bar with scrolling red bars."""
    
    def __init__(self, bar_width=None, style="green", complete_style="red", finished_style="green"):
        super().__init__(bar_width=bar_width, style=style)
        self.complete_style = complete_style
        self.finished_style = finished_style
        self._animation_offset = 0
    
    def render(self, task):
        """Render the animated progress bar."""
        if task.total is None or task.total == 0:
            return Text("", style=self.style)
        
        # Calculate progress
        progress = task.completed / task.total
        completed_width = int(progress * (self.bar_width or 40))
        
        # Create the base bar
        bar_text = "█" * completed_width + "░" * ((self.bar_width or 40) - completed_width)
        
        # Add animated red bars for incomplete portion
        if completed_width < (self.bar_width or 40):
            # Create scrolling red bars effect
            remaining_width = (self.bar_width or 40) - completed_width
            red_bars = "█" * min(3, remaining_width)  # 3-character red bars
            
            # Animate the red bars position
            self._animation_offset = (self._animation_offset + 1) % max(1, remaining_width - 2)
            
            # Insert red bars at animated position
            if remaining_width > 3:
                bar_list = list(bar_text)
                start_pos = completed_width + self._animation_offset
                for i, char in enumerate(red_bars):
                    pos = start_pos + i
                    if pos < len(bar_list) and bar_list[pos] == "░":
                        bar_list[pos] = char
                bar_text = "".join(bar_list)
        
        # Apply styling
        if task.finished:
            style = self.finished_style
        else:
            style = self.complete_style if completed_width > 0 else self.style
        
        return Text(bar_text, style=style)


class FFmpegDataColumn(ProgressColumn):
    """Custom column to display FFmpeg data for worker progress bars."""
    
    def render(self, task):
        # Get FFmpeg data from task fields
        frame = task.fields.get("frame", 0)
        fps = task.fields.get("fps", 0)
        time_str = task.fields.get("time_str", "00:00:00.00")
        speed = task.fields.get("speed", "0.0x")
        
        # Create simplified FFmpeg-style output with only essential info
        if frame > 0 or fps > 0:
            ffmpeg_data = f"frame={frame:4d} fps={fps:4.1f} time={time_str} speed={speed}"
            return Text(ffmpeg_data, style="dim")
        else:
            return Text("Waiting for FFmpeg data...", style="dim")


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Generate video preview thumbnails for Plex Media Server'
    )
    
    # Plex server configuration
    parser.add_argument('--plex-url', help='Plex server URL (e.g., http://localhost:32400)')
    parser.add_argument('--plex-token', help='Plex authentication token (get from https://support.plex.tv/articles/204059436/)')
    parser.add_argument('--plex-timeout', type=int, help='Plex API timeout in seconds (default: 60)')
    parser.add_argument('--plex-libraries', help='Comma-separated list of library names (e.g., "Movies, TV Shows")')
    
    # Media paths
    parser.add_argument('--plex-config-folder', help='Path to Plex Media Server configuration folder (e.g., /path_to/plex/Library/Application Support/Plex Media Server)')
    parser.add_argument('--plex-local-videos-path-mapping', help='Local videos path mapping (e.g., /path/this/script/sees/to/video/library)')
    parser.add_argument('--plex-videos-path-mapping', help='Plex videos path mapping (e.g., /path/plex/sees/to/video/library)')
    
    # Processing configuration
    parser.add_argument('--plex-bif-frame-interval', type=int, help='Interval between preview images in seconds (default: 5)')
    parser.add_argument('--thumbnail-quality', type=int, help='Preview image quality 1-10 (default: 4, 2=highest quality, 10=lowest quality)')
    parser.add_argument('--regenerate-thumbnails', action='store_true', help='Regenerate existing thumbnails (default: false)')
    
    # Threading configuration
    parser.add_argument('--gpu-threads', type=int, help='Number of GPU worker threads (default: 4)')
    parser.add_argument('--cpu-threads', type=int, help='Number of CPU worker threads (default: 4)')
    parser.add_argument('--gpu-selection', help='GPU selection: "all" or comma-separated indices like "0,1,2" (default: all)')
    parser.add_argument('--list-gpus', action='store_true', help='List detected GPUs and exit')
    
    # System paths
    parser.add_argument('--tmp-folder', help='Temporary folder for processing')
    
    # Logging
    parser.add_argument('--log-level', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'debug', 'info', 'warning', 'error'], help='Logging level (default: INFO)')
        
    return parser.parse_args()


def signal_handler(signum, frame):
    """Handle interrupt signals gracefully."""
    logger.info("Received interrupt signal, shutting down gracefully...")
    
    # Perform cleanup using global application state
    app_state.cleanup()
    
    sys.exit(0)


def list_gpus() -> None:
    """List detected GPUs and exit."""
    logger.info('🔍 Detecting available GPUs...')
    
    detected_gpus = detect_all_gpus()
    
    if not detected_gpus:
        logger.info('❌ No GPUs detected')
        logger.info('💡 Use --cpu-threads to run with CPU-only processing')
        return
    
    logger.info(f'✅ Found {len(detected_gpus)} GPU(s):')
    for i, (gpu_type, gpu_device, gpu_info) in enumerate(detected_gpus):
        gpu_name = gpu_info.get('name', f'{gpu_type} GPU')
        acceleration = gpu_info.get('acceleration', None)
        gpu_desc = format_gpu_info(gpu_type, gpu_device, gpu_name, acceleration)
        logger.info(f'  [{i}] {gpu_desc}')
    
    logger.info('')
    logger.info('💡 Use --gpu-selection "0,1" to select specific GPUs')
    logger.info('💡 Use --gpu-selection "all" to use all detected GPUs (default)')


def setup_application() -> tuple:
    """Set up logging, parse arguments, and handle special flags."""
    # Set up logging with default level first
    setup_logging(console=console)
    
    # Check for Windows and show info message
    if is_windows():
        logger.info('=' * 80)
        logger.info('🪟 Windows Platform Detected')
        logger.info('=' * 80)
        logger.info('')
        logger.info('GPU Support: D3D11VA hardware decode acceleration')
        logger.info('  • Works with NVIDIA, AMD, and Intel GPUs')
        logger.info('  • Requires compatible GPU and latest drivers')
        logger.info('')
        logger.info('Detecting available GPUs...')
        logger.info('')
    
    # Parse command-line arguments
    args = parse_arguments()
    
    # Apply log level from arguments if provided (before --list-gpus handling)
    if args.log_level:
        setup_logging(args.log_level.upper(), console=console)
    
    # Handle --list-gpus flag
    if args.list_gpus:
        list_gpus()
        return None, None
    
    logger.info('This project has been completely rewritten for better performance and reliability.')
    logger.info('Please report any issues at https://github.com/stevezau/plex_generate_vid_previews/issues')

    # Check for updates (non-blocking, fails gracefully)
    check_for_updates()

    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    # SIGTERM doesn't exist on Windows
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, signal_handler)

    # Load and validate configuration (CLI args take precedence over env vars)
    # Note: Basic logging is already set up, so config validation errors will be logged properly
    config = load_config(args)
    
    # Exit if configuration validation failed
    if config is None:
        sys.exit(1)
    
    # Store config in global application state for cleanup
    app_state.set_config(config)
    
    # Update logging level from config (in case it wasn't set in load_config)
    setup_logging(config.log_level, console=console)
    
    return args, config


def setup_working_directory(config) -> None:
    """Create and set up the working temporary directory."""
    try:
        config.working_tmp_folder = create_working_directory(config.tmp_folder)
        logger.debug(f"Created working temp folder: {config.working_tmp_folder}")
    except Exception as cleanup_error:
        logger.error(f"Failed to create working temp folder: {cleanup_error}")
        sys.exit(1)


def detect_and_select_gpus(config) -> list:
    """Detect available GPUs and select based on configuration."""
    selected_gpus = []
    
    if config.gpu_threads > 0:
        # Detect all available GPUs
        detected_gpus = detect_all_gpus()
        
        if not detected_gpus:
            logger.error('No GPUs detected.')
            logger.error('Please set the GPU_THREADS environment variable to 0 to use CPU-only processing.')
            logger.error('If you think this is an error please log an issue here https://github.com/stevezau/plex_generate_vid_previews/issues')
            sys.exit(1)
        
        # Display detected GPUs
        logger.info(f'🔍 Detected {len(detected_gpus)} GPU(s):')
        for i, (gpu_type, gpu_device, gpu_info) in enumerate(detected_gpus):
            gpu_name = gpu_info.get('name', f'{gpu_type} GPU')
            acceleration = gpu_info.get('acceleration', None)
            gpu_desc = format_gpu_info(gpu_type, gpu_device, gpu_name, acceleration)
            logger.info(f'  [{i}] {gpu_desc}')
        
        # Filter GPUs based on selection
        if config.gpu_selection.lower() == 'all':
            selected_gpus = detected_gpus
            logger.info(f'✅ Using all {len(selected_gpus)} GPU(s)')
            if len(detected_gpus) > 1:
                logger.info(f'💡 To use specific GPUs only, use --gpu-selection "0" or --gpu-selection "0,1"')
        else:
            try:
                # Parse GPU indices
                gpu_indices = [int(x.strip()) for x in config.gpu_selection.split(',') if x.strip()]
                selected_gpus = []
                
                for idx in gpu_indices:
                    if 0 <= idx < len(detected_gpus):
                        selected_gpus.append(detected_gpus[idx])
                    else:
                        logger.error(f'❌ GPU {idx} not found. Available GPUs: 0-{len(detected_gpus)-1}')
                        logger.error('💡 Run with --list-gpus to see available GPUs')
                        sys.exit(1)
                
                if not selected_gpus:
                    logger.error('❌ No valid GPUs selected')
                    sys.exit(1)
                
                logger.info(f'✅ Using {len(selected_gpus)} selected GPU(s): {config.gpu_selection}')
                
            except ValueError:
                logger.error(f'❌ Invalid GPU selection format: {config.gpu_selection}')
                logger.error('💡 Use "all" or comma-separated indices like "0,1,2"')
                sys.exit(1)
    else:
        logger.debug("GPU threads set to 0 - using CPU-only processing")
    
    return selected_gpus


def create_progress_displays():
    """Create progress display instances for different purposes."""
    # Create separate Progress instances for different purposes
    main_progress = Progress(
        SpinnerColumn(), 
        TextColumn("[bold green]{task.description}"),
        BarColumn(bar_width=None, style="red", complete_style="green", finished_style="green"),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        refresh_per_second=20
    )
    
    worker_progress = Progress(
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=None, style="cyan"),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        FFmpegDataColumn(),  # Show FFmpeg data instead of time
        console=console,
        refresh_per_second=20
    )
    
    # Special progress for querying library with animated bar
    query_progress = Progress(
        SpinnerColumn(), 
        TextColumn("[bold green]{task.description}"),
        AnimatedBarColumn(bar_width=None, style="green", complete_style="red", finished_style="green"),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        refresh_per_second=20
    )
    
    return main_progress, worker_progress, query_progress


def run_processing(config, selected_gpus):
    """Run the main processing workflow."""
    try:
        # Get Plex server
        plex = plex_server(config)
        
        # Calculate title width for display formatting
        title_max_width = calculate_title_width()
        
        # Create worker pool
        worker_pool = WorkerPool(
            gpu_workers=config.gpu_threads,
            cpu_workers=config.cpu_threads,
            selected_gpus=selected_gpus
        )
        
        # Process all library sections
        total_processed = 0
        
        # Create progress displays
        main_progress, worker_progress, query_progress = create_progress_displays()
        
        # Create a dynamic group that can switch between query and processing displays
        class DynamicGroup:
            def __init__(self):
                self.current_group = None
            
            def set_query_mode(self):
                self.current_group = Group(query_progress)
            
            def set_processing_mode(self):
                self.current_group = Group(main_progress, worker_progress)
            
            def __rich_console__(self, console, options):
                if self.current_group:
                    yield from self.current_group.__rich_console__(console, options)
        
        dynamic_group = DynamicGroup()
        
        with Live(dynamic_group, console=console, refresh_per_second=20):
            # Start in query mode
            dynamic_group.set_query_mode()
            query_task = query_progress.add_task("Querying library...", total=1, completed=0)
            
            # Get the generator for library sections
            library_sections = get_library_sections(plex, config)
            
            # Process all library sections
            for section, media_items in library_sections:
                if not media_items:
                    logger.info(f"No media items found in library '{section.title}', skipping")
                    continue
                
                # Switch to processing mode
                dynamic_group.set_processing_mode()
                query_progress.remove_task(query_task)
                
                main_task = main_progress.add_task(f"Processing {section.title}", total=len(media_items))
                
                # Process items in this section with worker progress
                worker_pool.process_items(media_items, config, plex, worker_progress, main_progress, main_task, title_max_width, library_name=section.title)
                total_processed += len(media_items)
                
                # Remove completed task
                main_progress.remove_task(main_task)
                
                # Switch back to query mode for next library
                dynamic_group.set_query_mode()
                query_task = query_progress.add_task("Querying library...", total=1, completed=0)
            
            # Remove final query task
            query_progress.remove_task(query_task)
        
        logger.info(f'Successfully processed {total_processed} media items across all libraries')
        
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down gracefully...")
    except ConnectionError as e:
        logger.error(f"Connection failed: {e}")
        logger.error("Please fix the connection issue and try again.")
        return 1
    except Exception as e:
        logger.error(f"Unexpected error in main execution: {e}")
        raise
    finally:
        # Clean up worker pool
        try:
            if 'worker_pool' in locals():
                worker_pool.shutdown()
        except Exception as worker_error:
            logger.warning(f"Failed to shutdown worker pool: {worker_error}")
        
        # Clean up our working temp folder
        try:
            if os.path.isdir(config.working_tmp_folder):
                shutil.rmtree(config.working_tmp_folder)
                logger.debug(f"Cleaned up working temp folder: {config.working_tmp_folder}")
        except Exception as cleanup_error:
            logger.warning(f"Failed to clean up working temp folder {config.working_tmp_folder}: {cleanup_error}")
        
        # Final terminal cleanup to ensure cursor is visible
        try:
            console.show_cursor(True)
            # Force Rich to restore the terminal to its original state
            if hasattr(console, '_live'):
                console._live = None
            # Ensure we're on a fresh line
            console.print()
        except:
            pass


def main() -> None:
    """Main entry point for the application."""
    # Set up application (logging, arguments, config)
    args, config = setup_application()
    if config is None:  # Handled --list-gpus flag
        return
    
    # Set up working directory
    setup_working_directory(config)
    
    # Detect and select GPUs
    selected_gpus = detect_and_select_gpus(config)
    
    # Run the main processing workflow
    run_processing(config, selected_gpus)


if __name__ == '__main__':
    main()
