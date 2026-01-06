"""
Plex watch mode for real-time monitoring of new items.

Handles plexapi AlertListener with proper error handling, reconnection logic,
and WebSocket timeout/error handling for watch mode.
"""

import time
import threading
from typing import Callable, Optional
from loguru import logger

# Hardcoded reconnect delay (30 seconds)
RECONNECT_DELAY = 10
MAX_RECONNECT_ATTEMPTS = 10


class AlertListenerWrapper:
    """
    Wrapper around plexapi AlertListener with automatic reconnection.
    
    Handles WebSocket timeouts, invalid responses, and connection failures
    with exponential backoff.
    
    IMPORTANT: library.new is a webhook event (HTTP POST), not a WebSocket event.
    AlertListener uses WebSocket connections, so we may NOT receive library.new events.
    
    Instead, we rely on:
    - media.scanner.finished events (when scanner completes)
    - activity alerts that indicate library scanning
    - Checking recently added items after scanner finishes via recentlyAdded API
    
    This approach is more reliable than expecting library.new via WebSocket.
    """
    
    def __init__(self, plex, callback: Callable, config):
        """
        Initialize AlertListener wrapper.
        
        Args:
            plex: Plex server instance
            callback: Callback function to handle alerts (item_key, alert_type)
            config: Configuration object
        """
        self.plex = plex
        self.callback = callback
        self.config = config
        self.listener = None
        self.running = False
        self.thread = None
        self.reconnect_attempts = 0
        self._lock = threading.Lock()
    
    def _handle_alert(self, data):
        """
        Handle incoming alert from Plex.
        
        Args:
            data: Alert data dictionary from plexapi AlertListener
        """
        try:
            # AlertListener callback receives a dict with 'NotificationContainer'
            # The actual notification data is in the NotificationContainer
            if isinstance(data, dict):
                # Extract from NotificationContainer if present
                notification = data.get('NotificationContainer', data)
                
                # Handle both dict and list structures
                if isinstance(notification, list) and len(notification) > 0:
                    # Sometimes it's a list with the notification as first element
                    notification = notification[0] if isinstance(notification[0], dict) else data
                elif not isinstance(notification, dict):
                    notification = data
                
                # Extract alert type
                alert_type = notification.get('type') or notification.get('Type') or notification.get('notificationType')
                
                # Log all alert types at debug level to see what we're receiving
                logger.debug(f"Received alert type: {alert_type}")
                
                # Inspect activity alerts - they may contain library scanning information
                # Plex often sends activity alerts during library scanning instead of library.new
                if alert_type == 'activity':
                    # Activity alerts can indicate library scanning
                    activity_type = notification.get('activity') or notification.get('Activity')
                    activity_subtype = notification.get('subtype') or notification.get('Subtype')
                    activity_key = notification.get('key') or notification.get('Key')
                    activity_title = notification.get('title') or notification.get('Title')
                    activity_context = notification.get('Context') or notification.get('context')
                    
                    logger.debug(f"Activity alert - type: {activity_type}, subtype: {activity_subtype}, key: {activity_key}, title: {activity_title}, context: {activity_context}")
                    
                    # Check if this activity is related to library scanning or content updates
                    # Plex sends activity alerts for various library operations
                    is_scanning_activity = (
                        activity_type in ('scanning', 'refreshing', 'updating', 'library.scanning', 'library.refreshing') or
                        activity_subtype in ('scanning', 'refreshing', 'updating', 'library.scanning', 'library.refreshing') or
                        'library' in str(activity_type).lower() or
                        'scan' in str(activity_type).lower() or
                        'scan' in str(activity_subtype).lower() or
                        'refresh' in str(activity_type).lower() or
                        'refresh' in str(activity_subtype).lower() or
                        (activity_key and '/library' in str(activity_key)) or
                        (activity_title and ('scan' in str(activity_title).lower() or 'refresh' in str(activity_title).lower())) or
                        (activity_context and 'library' in str(activity_context).lower())
                    )
                    
                    if is_scanning_activity:
                        logger.info(f"Library scanning activity detected ({activity_type}/{activity_subtype}) - will check for recently added items after scanner finishes")
                        # Trigger check for recently added items when scanning activity detected
                        # Use a delay to wait for scanner to finish and Plex to process metadata
                        # We'll check for recently added items after the scanner completes
                        import threading
                        def delayed_check():
                            # Wait for scanner to finish processing (Plex needs time to create metadata)
                            time.sleep(5)  # Wait 5 seconds for Plex to finish processing and create metadata
                            logger.info("Checking for recently added items after library scanning activity")
                            self.callback(None, 'media.scanner.finished')
                        threading.Thread(target=delayed_check, daemon=True).start()
                        return
                
                # For media.scanner.finished, we should check recently added items
                # since the scanner doesn't always include the item key in the alert
                # NOTE: library.new is a webhook event, not a WebSocket event
                # AlertListener uses WebSocket, so we may not receive library.new events
                # Instead, we rely on media.scanner.finished and activity detection
                if alert_type == 'media.scanner.finished':
                    logger.info("Scanner finished - will check for recently added items")
                    # For scanner finished, we don't have an item key, so we'll trigger
                    # a check of recently added items. The callback will handle this.
                    # Pass None as item_key to indicate we should check recently added
                    self.callback(None, 'media.scanner.finished')
                    return
                
                # Process library.new for new items (if received via WebSocket)
                # NOTE: library.new is primarily a webhook event, not WebSocket
                # Some Plex versions may send it via WebSocket, but it's unreliable
                if alert_type == 'library.new':
                    logger.info(f"Received library.new alert - processing new item")
                    # Continue to extract item key below
                # For update.statechange, check if it's a relevant state change
                elif alert_type == 'update.statechange':
                    # Check if this is a state change that indicates new/replaced content
                    state = notification.get('state') or notification.get('State')
                    logger.debug(f"State change alert - state: {state}")
                    # Process if state is 'added' (new item) or potentially 'updated' (replaced file)
                    if state in ('added', 'updated'):
                        logger.info(f"Received update.statechange with state '{state}' - processing item")
                        # Continue to extract item key below
                    else:
                        logger.debug(f"Ignoring state change: {state}")
                        return
                else:
                    logger.debug(f"Ignoring alert type: {alert_type}")
                    return
                
                # Extract item key from notification
                # Try multiple possible keys
                item_key = None
                item_key = (notification.get('itemKey') or notification.get('key') or 
                           notification.get('ItemKey') or notification.get('Key'))
                
                # If no itemKey, try to construct from ratingKey
                if not item_key:
                    rating_key = (notification.get('ratingKey') or notification.get('RatingKey') or
                                 notification.get('rating_key'))
                    if rating_key:
                        item_key = f"/library/metadata/{rating_key}"
                
                # Also check for MetadataItem key
                if not item_key:
                    metadata_item = notification.get('MetadataItem') or notification.get('metadataItem')
                    if isinstance(metadata_item, dict):
                        item_key = metadata_item.get('key') or metadata_item.get('Key')
                        if not item_key and 'ratingKey' in metadata_item:
                            item_key = f"/library/metadata/{metadata_item.get('ratingKey')}"
                
                if not item_key:
                    logger.warning(f"Could not extract item key from alert: {notification}")
                    logger.debug(f"Alert keys: {list(notification.keys()) if isinstance(notification, dict) else 'not dict'}")
                    logger.debug(f"Full notification data: {notification}")
                    # For update.statechange, try to get ratingKey from the update
                    if alert_type == 'update.statechange':
                        update_item = notification.get('Update') or notification.get('update')
                        if isinstance(update_item, dict):
                            rating_key = update_item.get('ratingKey') or update_item.get('RatingKey')
                            if rating_key:
                                item_key = f"/library/metadata/{rating_key}"
                                logger.info(f"Extracted item key from update: {item_key}")
                    
                    if not item_key:
                        return
                
                logger.info(f"✅ Received {alert_type} alert for item: {item_key}")
                
                # Call the callback with item key and alert type
                self.callback(item_key, alert_type)
            else:
                # Handle object structure (fallback)
                alert_type = getattr(data, 'type', None) or getattr(data, 'Type', None)
                
                # Log all alert types at debug level
                logger.debug(f"Received alert type: {alert_type}")
                
                # Inspect activity alerts - they may contain library scanning information
                if alert_type == 'activity':
                    # Activity alerts can indicate library scanning
                    activity_type = getattr(data, 'activity', None) or getattr(data, 'Activity', None)
                    activity_subtype = getattr(data, 'subtype', None) or getattr(data, 'Subtype', None)
                    activity_key = getattr(data, 'key', None) or getattr(data, 'Key', None)
                    activity_title = getattr(data, 'title', None) or getattr(data, 'Title', None)
                    activity_context = getattr(data, 'Context', None) or getattr(data, 'context', None)
                    
                    logger.debug(f"Activity alert - type: {activity_type}, subtype: {activity_subtype}, key: {activity_key}, title: {activity_title}, context: {activity_context}")
                    
                    # Check if this activity is related to library scanning
                    is_scanning_activity = (
                        activity_type in ('scanning', 'refreshing', 'updating', 'library.scanning', 'library.refreshing') if activity_type else False or
                        activity_subtype in ('scanning', 'refreshing', 'updating', 'library.scanning', 'library.refreshing') if activity_subtype else False or
                        ('library' in str(activity_type).lower()) if activity_type else False or
                        ('scan' in str(activity_type).lower()) if activity_type else False or
                        ('scan' in str(activity_subtype).lower()) if activity_subtype else False or
                        ('refresh' in str(activity_type).lower()) if activity_type else False or
                        ('refresh' in str(activity_subtype).lower()) if activity_subtype else False or
                        (activity_key and '/library' in str(activity_key)) or
                        (activity_title and ('scan' in str(activity_title).lower() or 'refresh' in str(activity_title).lower())) or
                        (activity_context and 'library' in str(activity_context).lower())
                    )
                    
                    if is_scanning_activity:
                        logger.info(f"Library scanning activity detected ({activity_type}/{activity_subtype}) - will check for recently added items")
                        # Trigger check for recently added items when scanning activity detected
                        # Use a small delay to ensure Plex has finished processing
                        def delayed_check():
                            time.sleep(2)  # Wait 2 seconds for Plex to finish processing
                            self.callback(None, 'media.scanner.finished')
                        threading.Thread(target=delayed_check, daemon=True).start()
                        return
                
                # For media.scanner.finished, trigger check for recently added items
                if alert_type == 'media.scanner.finished':
                    logger.info("Scanner finished - will check for recently added items")
                    self.callback(None, 'media.scanner.finished')
                    return
                
                # Process library.new for new items
                if alert_type == 'library.new':
                    logger.info(f"Received library.new alert - processing new item")
                    # Continue to extract item key below
                # For update.statechange, check if it's a relevant state change
                elif alert_type == 'update.statechange':
                    state = getattr(data, 'state', None) or getattr(data, 'State', None)
                    logger.debug(f"State change alert - state: {state}")
                    if state in ('added', 'updated'):
                        logger.info(f"Received update.statechange with state '{state}' - processing item")
                        # Continue to extract item key below
                    else:
                        logger.debug(f"Ignoring state change: {state}")
                        return
                else:
                    logger.debug(f"Ignoring alert type: {alert_type}")
                    return
                
                item_key = None
                if hasattr(data, 'itemKey'):
                    item_key = data.itemKey
                elif hasattr(data, 'key'):
                    item_key = data.key
                elif hasattr(data, 'ratingKey'):
                    rating_key = data.ratingKey
                    if rating_key:
                        item_key = f"/library/metadata/{rating_key}"
                
                if item_key:
                    logger.info(f"✅ Received {alert_type} alert for item: {item_key}")
                    self.callback(item_key, alert_type)
                else:
                    logger.warning(f"Could not extract item key from alert: {data}")
                    # For update.statechange, try to get ratingKey from the update
                    if alert_type == 'update.statechange':
                        update_item = getattr(data, 'Update', None) or getattr(data, 'update', None)
                        if update_item:
                            rating_key = getattr(update_item, 'ratingKey', None) or getattr(update_item, 'RatingKey', None)
                            if rating_key:
                                item_key = f"/library/metadata/{rating_key}"
                                logger.info(f"Extracted item key from update: {item_key}")
                                self.callback(item_key, alert_type)
                                return
                    logger.debug(f"Full alert data: {data}")
            
        except Exception as e:
            logger.error(f"Error handling alert: {e}")
            logger.debug(f"Alert data: {data}", exc_info=True)
    
    def _run_listener(self):
        """Run the AlertListener in a separate thread with reconnection logic."""
        while self.running:
            try:
                # Import AlertListener here to avoid import errors if not available
                from plexapi.alert import AlertListener
                
                logger.info("Starting AlertListener connection...")
                logger.debug(f"Plex server URL: {self.plex._baseurl if hasattr(self.plex, '_baseurl') else 'unknown'}")
                logger.debug(f"Plex server token: {'***' if hasattr(self.plex, '_token') else 'missing'}")
                
                self.listener = AlertListener(self.plex, callback=self._handle_alert)
                logger.debug("AlertListener instance created successfully")
                
                # Start the listener (this blocks until connection fails)
                logger.debug("Calling AlertListener.start()...")
                try:
                    # Inspect AlertListener before starting
                    logger.debug(f"AlertListener type: {type(self.listener)}")
                    logger.debug(f"AlertListener attributes: {[attr for attr in dir(self.listener) if not attr.startswith('__')]}")
                    
                    # Check if websocket exists before starting
                    if hasattr(self.listener, '_ws'):
                        logger.debug(f"AlertListener websocket before start: {self.listener._ws}")
                    
                    self.listener.start()
                    
                    # AlertListener.start() returns immediately after creating the WebSocket.
                    # The WebSocket connection is established asynchronously. Give it a moment to connect.
                    time.sleep(0.5)
                    
                    # Check if websocket was created
                    if hasattr(self.listener, '_ws'):
                        ws = self.listener._ws
                        if ws is not None:
                            # WebSocket was created - connection is successful!
                            logger.success("✓ AlertListener WebSocket connection established")
                            logger.debug(f"AlertListener websocket: {ws}")
                            
                            # Reset reconnect attempts since we successfully connected
                            self.reconnect_attempts = 0
                            
                            # Wait for the listener thread to finish (this blocks until connection fails)
                            # AlertListener runs in its own thread, so we wait for it to stop
                            if hasattr(self.listener, '_thread') and self.listener._thread:
                                logger.debug("Waiting for AlertListener thread...")
                                # Wait for the thread to finish (blocks until connection fails)
                                self.listener._thread.join()
                                logger.warning("AlertListener thread stopped - connection lost")
                            else:
                                # If no thread attribute, check if it's running as a daemon thread
                                # AlertListener extends Thread, so it IS the thread
                                if hasattr(self.listener, 'is_alive') and self.listener.is_alive():
                                    logger.debug("AlertListener is running as thread")
                                    # Wait for it to finish
                                    self.listener.join()
                                    logger.warning("AlertListener thread stopped - connection lost")
                                else:
                                    # Thread might have already stopped
                                    logger.debug("AlertListener thread not found or already stopped")
                                    # Continue to reconnect logic below
                        else:
                            # WebSocket is None - connection failed
                            logger.error("⚠️ AlertListener websocket is None - connection was never established")
                            logger.error("")
                            logger.error("Possible causes:")
                            logger.error("  1. Plex server version does not support WebSocket/AlertListener API")
                            logger.error("     → AlertListener requires Plex Media Server 1.7.0+")
                            logger.error("  2. Plex server has WebSocket/Event Notifications disabled")
                            logger.error("     → Check Settings → Network → Enable Remote Access")
                            logger.error("  3. Network/firewall blocking WebSocket connections")
                            logger.error("     → WebSocket uses same port as Plex (32400) but different protocol")
                            logger.error("  4. Plex server URL is incorrect or using wrong protocol")
                            logger.error(f"     → Current URL: {self.plex._baseurl if hasattr(self.plex, '_baseurl') else 'unknown'}")
                            logger.error("")
                            logger.error("💡 Solution: Use scheduled mode instead:")
                            logger.error("   --daemon scheduled  (or -d scheduled)")
                            logger.error("")
                            logger.error("This will check for new items periodically instead of real-time monitoring.")
                    else:
                        logger.debug("AlertListener does not have _ws attribute")
                        # Continue to delay and reconnect logic
                    
                    if hasattr(self.listener, '_running'):
                        logger.debug(f"AlertListener running state: {self.listener._running}")
                    if hasattr(self.listener, '_thread'):
                        logger.debug(f"AlertListener thread: {self.listener._thread}")
                    
                except Exception as start_error:
                    # start() raised an exception - this is the actual connection failure
                    error_type = type(start_error).__name__
                    logger.error(f"AlertListener.start() failed ({error_type}): {start_error}")
                    logger.debug(f"AlertListener.start() exception details:", exc_info=True)
                    
                    # Re-raise to be caught by outer exception handler
                    raise start_error
                # Note: If we get here, the listener thread stopped.
                # The reconnect attempts will be reset if we successfully connected above.
                # If websocket was None, we'll continue with the reconnection logic below.
                
            except Exception as e:
                if not self.running:
                    # We're shutting down, don't try to reconnect
                    break
                
                error_type = type(e).__name__
                logger.error(f"AlertListener error ({error_type}): {e}")
                logger.debug(f"Full exception details:", exc_info=True)
                
                # Provide more helpful error messages based on exception type
                if 'Connection' in error_type or 'Connect' in error_type:
                    logger.error("Connection failed - check Plex server is accessible and network connectivity")
                elif 'Timeout' in error_type:
                    logger.error("Connection timeout - Plex server may be slow or unreachable")
                elif 'SSL' in error_type or 'Certificate' in error_type:
                    logger.error("SSL/TLS error - check certificate settings")
                elif 'WebSocket' in error_type or 'WS' in error_type:
                    logger.error("WebSocket error - AlertListener may not be supported by this Plex server version")
                else:
                    logger.debug(f"Unknown error type: {error_type}")
                
                # Don't reset attempts on exception - we never successfully connected
            
            # Apply delay whether we got here from exception or normal return
            if not self.running:
                break
                
            # Check if we should give up
            if self.reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
                logger.error(f"AlertListener failed after {MAX_RECONNECT_ATTEMPTS} reconnect attempts")
                logger.error("Falling back to scheduled mode may be required")
                break
            
            # Calculate delay with exponential backoff
            delay = RECONNECT_DELAY * (2 ** min(self.reconnect_attempts, 5))  # Cap at 32x delay
            self.reconnect_attempts += 1
            
            logger.info(f"Reconnecting AlertListener in {delay} seconds (attempt {self.reconnect_attempts}/{MAX_RECONNECT_ATTEMPTS})...")
            
            # Wait for reconnect delay, but check if we should stop frequently
            # Use shorter sleep intervals to be more responsive to shutdown
            sleep_interval = 0.5  # Check every 0.5 seconds instead of 1 second
            iterations = int(delay / sleep_interval)
            for _ in range(iterations):
                if not self.running:
                    return
                time.sleep(sleep_interval)
    
    def start(self):
        """Start the AlertListener in a background thread."""
        with self._lock:
            if self.running:
                logger.warning("AlertListener is already running")
                return
            
            self.running = True
            self.reconnect_attempts = 0
            
            # Check if plexapi has AlertListener
            try:
                from plexapi.alert import AlertListener
            except ImportError:
                logger.error("plexapi AlertListener not available. Please ensure you have a recent version of plexapi.")
                raise ImportError("plexapi AlertListener not available")
            
            self.thread = threading.Thread(target=self._run_listener, daemon=True)
            self.thread.start()
            logger.info("AlertListener started in background thread")
    
    def stop(self):
        """Stop the AlertListener gracefully."""
        with self._lock:
            if not self.running:
                return
            
            logger.info("Stopping AlertListener...")
            self.running = False
            
            # Stop the listener if it exists
            if self.listener:
                try:
                    # AlertListener.stop() may try to close a websocket that doesn't exist
                    # if the connection was never established or failed early
                    self.listener.stop()
                except (AttributeError, TypeError) as e:
                    # Handle case where AlertListener internals aren't initialized
                    logger.debug(f"AlertListener stop called but connection not fully initialized: {e}")
                except Exception as e:
                    logger.warning(f"Error stopping AlertListener: {e}")
            
            # Wait for thread to finish (with timeout)
            if self.thread and self.thread.is_alive():
                self.thread.join(timeout=5)
                if self.thread.is_alive():
                    logger.warning("AlertListener thread did not stop within timeout")
            
            logger.info("AlertListener stopped")
    
    def is_running(self) -> bool:
        """Check if AlertListener is running."""
        return self.running and (self.thread is not None and self.thread.is_alive())

