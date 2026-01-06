"""
Service daemon that orchestrates AlertListener and scheduled scanning.

Handles daemon mode with proper signal handling and graceful shutdown.
"""

import signal
import sys
import time
import threading
from typing import Optional
from loguru import logger

from .config import Config
from .plex_client import plex_server
from .plex_watch import AlertListenerWrapper
from .scheduler import Scheduler
from .gpu_detection import detect_all_gpus, format_gpu_info
from .worker import WorkerPool
from .media_processing import process_item
from .utils import calculate_title_width


class DaemonService:
    """
    Daemon service that processes media items in real-time or on schedule.
    """
    
    def __init__(self, config: Config, selected_gpus):
        """
        Initialize daemon service.
        
        Args:
            config: Configuration object
            selected_gpus: List of selected GPUs
        """
        self.config = config
        self.selected_gpus = selected_gpus
        self.plex = None
        self.alert_listener = None
        self.scheduler = None
        self.worker_pool = None
        self.running = False
        self._lock = threading.Lock()
    
    def _process_item_callback(self, item_key: str, alert_type: str = None):
        """
        Callback to process a single item from AlertListener.
        
        Args:
            item_key: Plex media item key (None for media.scanner.finished to check recently added)
            alert_type: Type of alert (library.new, media.scanner.finished, update.statechange)
        """
        try:
            # For media.scanner.finished, we don't have a specific item key
            # so we need to check recently added items
            if alert_type == 'media.scanner.finished' and item_key is None:
                logger.info("Scanner finished or library scanning detected - checking for recently added items")
                from .scheduler import get_recently_added_items
                
                # Get recently added items (full_scan=False uses recentlyAdded endpoint)
                # This will get items added in the last few minutes
                recently_added = list(get_recently_added_items(self.plex, self.config, full_scan=False))
                
                if recently_added:
                    logger.info(f"Found {len(recently_added)} recently added items")
                    # Process each recently added item
                    for section, media_items in recently_added:
                        if not media_items:
                            continue
                        logger.info(f"Processing {len(media_items)} items from library '{section.title}'")
                        for key, title, media_type in media_items:
                            logger.info(f"Processing recently added item: {title} ({key})")
                            # Get GPU info for processing
                            gpu = None
                            gpu_device = None
                            if self.selected_gpus and len(self.selected_gpus) > 0:
                                gpu_type, gpu_device_path, _ = self.selected_gpus[0]
                                gpu = gpu_type
                                gpu_device = gpu_device_path
                            
                            # Process the item
                            process_item(key, gpu, gpu_device, self.config, self.plex)
                else:
                    logger.debug("No recently added items found - may have already been processed or nothing new was added")
                return
            
            # For library.new and update.statechange, we have a specific item key
            if item_key is None:
                logger.warning(f"Received {alert_type} alert but no item key provided")
                return
            
            logger.info(f"Processing item from alert: {item_key} (alert_type: {alert_type})")
            
            # Get GPU info for processing
            gpu = None
            gpu_device = None
            if self.selected_gpus and len(self.selected_gpus) > 0:
                gpu_type, gpu_device_path, _ = self.selected_gpus[0]
                gpu = gpu_type
                gpu_device = gpu_device_path
            
            # Process the item
            process_item(item_key, gpu, gpu_device, self.config, self.plex)
            
        except Exception as e:
            logger.error(f"Error processing item {item_key}: {e}")
            logger.debug(f"Exception type: {type(e).__name__}", exc_info=True)
    
    def _process_section_callback(self, section, media_items):
        """
        Callback to process items from scheduler.
        
        Args:
            section: Plex library section
            media_items: List of tuples (key, title, media_type)
        """
        try:
            logger.info(f"Processing {len(media_items)} items from library '{section.title}'")
            
            # Process items using worker pool
            if not self.worker_pool:
                logger.error("Worker pool not initialized")
                return
            
            # Create minimal progress displays for daemon mode
            from rich.console import Console
            from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
            
            console = Console()
            progress = Progress(
                SpinnerColumn(),
                TextColumn("[bold green]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                console=console,
                disable=True  # Disable progress in daemon mode
            )
            
            worker_progress = Progress(
                TextColumn("{task.description}"),
                console=console,
                disable=True  # Disable progress in daemon mode
            )
            
            title_max_width = calculate_title_width()
            
            # Process items
            self.worker_pool.process_items(
                media_items, 
                self.config, 
                self.plex, 
                worker_progress, 
                progress, 
                None, 
                title_max_width, 
                library_name=section.title
            )
            
        except Exception as e:
            logger.error(f"Error processing section {section.title}: {e}")
            logger.debug(f"Exception type: {type(e).__name__}", exc_info=True)
    
    def start(self):
        """Start the daemon service."""
        with self._lock:
            if self.running:
                logger.warning("Daemon service is already running")
                return
            
            logger.info("Starting daemon service...")
            
            # Connect to Plex
            try:
                self.plex = plex_server(self.config)
            except Exception as e:
                logger.error(f"Failed to connect to Plex server: {e}")
                raise
            
            # Create worker pool
            self.worker_pool = WorkerPool(
                gpu_workers=self.config.gpu_threads,
                cpu_workers=self.config.cpu_threads,
                selected_gpus=self.selected_gpus
            )
            
            # Determine mode ('watch' or 'scheduled')
            mode = self.config.daemon_mode.lower() if self.config.daemon_mode else 'watch'
            
            if mode == 'watch':
                # Watch mode: watch for new items only (no fallback to scheduled)
                logger.info("Watch mode: Connecting to monitor for new items...")
                self.alert_listener = AlertListenerWrapper(
                    self.plex, 
                    self._process_item_callback, 
                    self.config
                )
                self.alert_listener.start()
                
                # Wait a bit to see if it connects
                time.sleep(2)
                
                if self.alert_listener.is_running():
                    logger.info("Watch mode active - monitoring for new items")
                    self.running = True
                    return  # Success - exit early
                else:
                    logger.error("Watch mode failed to connect. Please check your Plex connection or use --daemon scheduled for periodic scans.")
                    raise RuntimeError("Watch mode failed to connect")
            
            elif mode == 'scheduled':
                # Scheduled mode: periodic checks only
                self.scheduler = Scheduler(
                    self.plex,
                    self.config,
                    self._process_section_callback,
                    self.config.full_scan
                )
                self.scheduler.start()
                logger.info(f"Scheduled mode active - checking every {self.config.scan_interval} minutes")
                self.running = True
            
            else:
                logger.error(f"Invalid daemon mode: {mode}")
                raise ValueError(f"Invalid daemon mode: {mode}")
    
    def stop(self):
        """Stop the daemon service gracefully."""
        with self._lock:
            if not self.running:
                return
            
            logger.info("Stopping daemon service...")
            self.running = False
            
            # Stop AlertListener
            if self.alert_listener:
                self.alert_listener.stop()
                self.alert_listener = None
            
            # Stop scheduler
            if self.scheduler:
                self.scheduler.stop()
                self.scheduler = None
            
            # Shutdown worker pool
            if self.worker_pool:
                self.worker_pool.shutdown()
                self.worker_pool = None
            
            logger.info("Daemon service stopped")
    
    def is_running(self) -> bool:
        """Check if daemon service is running."""
        return self.running
    
    def run(self):
        """Run the daemon service (blocking until stopped)."""
        try:
            self.start()
            
            if not self.running:
                logger.error("Failed to start daemon service")
                return
            
            # Keep running until interrupted
            logger.info("Daemon service running... (Press Ctrl+C to stop)")
            
            while self.running:
                time.sleep(1)
                
                # Check if watch mode or scheduled mode is still running
                if self.alert_listener and not self.alert_listener.is_running():
                    logger.warning("Watch mode stopped, attempting restart...")
                    try:
                        self.alert_listener.start()
                        time.sleep(2)
                        if not self.alert_listener.is_running():
                            logger.error("Watch mode failed to restart. Please check your Plex connection.")
                            break
                    except Exception as e:
                        logger.error(f"Failed to restart watch mode: {e}")
                        logger.error("Watch mode unavailable. Please run a separate container with --daemon scheduled for periodic scans.")
                        break
                
                if self.scheduler and not self.scheduler.is_running():
                    logger.warning("Scheduled mode stopped, attempting restart...")
                    try:
                        self.scheduler.start()
                    except Exception as e:
                        logger.error(f"Failed to restart scheduled mode: {e}")
                        break
        
        except KeyboardInterrupt:
            logger.info("Received interrupt signal, shutting down...")
        except Exception as e:
            logger.error(f"Daemon service error: {e}")
            logger.debug(f"Exception type: {type(e).__name__}", exc_info=True)
        finally:
            self.stop()


def run_daemon_service(config: Config, selected_gpus):
    """
    Run the daemon service.
    
    Args:
        config: Configuration object
        selected_gpus: List of selected GPUs
    """
    # Set up signal handlers
    service = DaemonService(config, selected_gpus)
    
    def signal_handler(signum, frame):
        """Handle interrupt signals."""
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        service.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, signal_handler)
    
    # Run the service
    service.run()

