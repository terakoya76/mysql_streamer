from datetime import datetime

from pymysqlreplication import BinLogStreamReader
from pymysqlreplication.row_event import UpdateRowsEvent
from yelp_conn.connection_set import ConnectionSet

from replication_handler import config
from replication_handler.util.misc import HEARTBEAT_DB
from replication_handler.util.position import HeartbeatPosition


class HeartbeatSearcher(object):
    """Component which locates the log position of a heartbeat event in a mysql
    binary log given its sequence number.

    To use from other modules:
        pos = MySQLHeartbeatSearch().get_position({heartbeat_sequence_num})
    Returns a replication_handler.util.position.HeartbeatPosition object
        or None if it wasnt found
    """

    def __init__(self, db_cnct=None):
        # Set up database configuration info and connection
        source_config = config.source_database_config.entries[0]
        self.connection_config = {
            'host': source_config['host'],
            'port': source_config['port'],
            'user': source_config['user'],
            'passwd': source_config['passwd']
        }
        if db_cnct is None:
            self.db_cnct = ConnectionSet.rbr_source_ro().refresh_primary
        else:
            self.db_cnct = db_cnct

        # Load in a list of every log file
        self.all_logs = self._get_log_file_list()

        # Log_pos integer which corresponds to the final log_pos in the final log_file
        self.final_log_pos = self._get_last_log_position(self.all_logs[-1])

    def get_position(self, hb_timestamp_epoch, hb_serial):
        """Entry method for using the class from other python modules, which
        returns the HeartbeatPosition object.
        """
        hb_timestamp = datetime.fromtimestamp(hb_timestamp_epoch)
        start_index = self._binary_search_log_files(hb_timestamp, 0, len(self.all_logs))
        return self._full_search_log_file(start_index, hb_timestamp, hb_serial)

    def _is_heartbeat(self, event):
        """Returns whether or not a binlog event is a heartbeat event. A hearbeat
        event can only be a update row event.
        """
        return isinstance(event, UpdateRowsEvent) and event.schema == HEARTBEAT_DB

    def _get_log_file_list(self):
        """Returns a list of all the log files names on the configured
        db connection
        """
        cursor = self.db_cnct.cursor()
        cursor.execute('SHOW BINARY LOGS;')
        names = []
        for row in cursor.fetchall():
            names.append(row[0])
        return names

    def _get_last_log_position(self, binlog):
        """Returns the end log position of the final log entry in the requested
        binlog. This process isn't exactly free so it is used as little as
        possible in the search.
        """
        cursor = self.db_cnct.cursor()
        cursor.execute('SHOW BINLOG EVENTS IN \'{}\';'.format(binlog))
        # Each event is a tuple of the form
        # (0:Log_name 1:Pos 2:Event_type 3:Server_id 4:End_log_pos 5:Info)
        return cursor.fetchall()[-1][4]

    def _reaches_bound(self, current_log, current_position):
        """Returns true if the stream has hit the last element of the last log
        and needs to be manually closed.
        current_position should be the end_log_pos of the event being executed
        """
        if current_log != self.all_logs[-1]:
            return False
        if current_position == self.final_log_pos:
            return True
        return False

    def _open_stream(self, start_file="mysql-bin.000001", start_pos=4):
        """Returns a binary log stream starting at the given file and directly
        after the given position. server_id and blocking are both set here
        but they appear to have no effect on the actual stream.
        start_pos defaults to 4 because the first event in every binlog starts
        at log_pos 4.
        """
        return BinLogStreamReader(
            connection_settings=self.connection_config,
            server_id=1,
            blocking=False,
            resume_stream=True,
            log_file=start_file,
            log_pos=start_pos
        )

    #def _is_same_hb_timestamp_in_last_binlog(start_file, start_pos=4):

    def _get_first_heartbeat(self, start_file, start_pos=4):
        """Returns the first heartbeat we find after the given start_file
        and start_position -> HeartbeatPosition or None if there are no
        heartbeats after.
        """
        stream = self._open_stream(start_file, start_pos)
        for event in stream:
            if self._reaches_bound(stream.log_file, stream.log_pos) and not self._is_heartbeat(event):
                stream.close()
                return None
            if not self._is_heartbeat(event):
                continue
            stream.close()
            return HeartbeatPosition(
                hb_serial=event.rows[0]["after_values"]["serial"],
                hb_timestamp=event.rows[0]["after_values"]["timestamp"],
                log_file=stream.log_file,
                log_pos=stream.log_pos,
            )

    def _binary_search_log_files(self, target_timestamp, left_bound, right_bound):
        """Recursive binary search to determine the log file in which timestamp matches
        target values. Returns the index of the file it should be in
        """
        # Binary search base case in which a single log file is found
        if left_bound >= right_bound - 1:
            return left_bound
        mid = (left_bound + right_bound) / 2
        first_in_file = self._get_first_heartbeat(self.all_logs[mid])

        # _get_first_hb searches from the midpoint to the very end of all
        # the logs regardless of what right_bound is set to. so if it returns
        # None that means there are no hbs at all after mid, so we proceed
        # the search below mid
        if first_in_file is None:
            return self._binary_search_log_files(target_timestamp, target_serial, left_bound, mid)

        # otherwise, we can do a typical binary search with the result we found
        found_timestamp = first_in_file.hb_timestamp
        actual_file = self.all_logs.index(first_in_file.log_file)

        if found_timestamp == target_timestamp:
            return actual_file
        elif found_timestamp > target_timestamp:
            return self._binary_search_log_files(target_timestamp, left_bound, mid)
        else:
            # because the streams we open continue reading after reaching the end of a file,
            # we can speed up the search by setting the left bound to the actual file
            # the heartbeat was found in instead of the midpoint like a
            # traditional binsearch
            return self._binary_search_log_files(target_timestamp, actual_file, right_bound)

    def _full_search_log_file(self, start_index, hb_timestamp, hb_serial, start_pos=4):
        """ Search heartbeat which has given time stamp and serial. Search forward first.
        If target hb is not found, search backward.
        """
        hb_position = self._search_heartbeat_sequentially(
            self.all_logs[start_index],
            hb_timestamp,
            hb_serial
        )
        file_index = start_index - 1
        while file_index >= 0 and hb_position is None:
            found_hb, hb_position = self._search_last_hb_in_previous_file(
                self.all_logs[file_index],
                hb_timestamp,
                hb_serial
            )
            file_index -= 1
            if found_hb:
                break
        return hb_position

    def _search_heartbeat_sequentially(self, start_file, hb_timestamp, hb_serial, start_pos=4):
        """ Search the heartbeat sequentially from given location."""
        stream = self._open_stream(start_file, start_pos)
        for event in stream:
            # break if it is end of binlog
            if self._reaches_bound(stream.log_file, stream.log_pos) and not self._is_heartbeat(event):
                break;
            if not self._is_heartbeat(event):
                continue
            # break if we encounter heartheat with larger time stamp
            if event.rows[0]["after_values"]["timestamp"] > hb_timestamp:
                break
            if (
                event.rows[0]["after_values"]["timestamp"] != hb_timestamp or
                event.rows[0]["after_values"]["serial"] != hb_serial
            ):
                continue

            stream.close()
            return HeartbeatPosition(
                hb_serial=event.rows[0]["after_values"]["serial"],
                hb_timestamp=event.rows[0]["after_values"]["timestamp"],
                log_file=stream.log_file,
                log_pos=stream.log_pos,
            )
        stream.close()
        return None

    def _search_last_hb_in_previous_file(
        self,
        start_file,
        hb_timestamp,
        hb_serial,
        start_pos=4
    ):
        # found the hb in previous log file. If any hb is found, found_hb is true.
        # If hb with given time stamp and serial is found, target_hb holds it.
        stream = self._open_stream(start_file, start_pos)
        found_hb = False
        for event in stream:
            if stream.log_file != start_file:
                break;
            if not self._is_heartbeat(event):
                continue

            found_hb = True
            if event.rows[0]["after_values"]["timestamp"] == hb_timestamp and \
                event.rows[0]["after_values"]["serial"] == hb_serial:
                stream.close()
                return found_hb, HeartbeatPosition(
                    hb_serial=event.rows[0]["after_values"]["serial"],
                    hb_timestamp=event.rows[0]["after_values"]["timestamp"],
                    log_file=stream.log_file,
                    log_pos=stream.log_pos,
                )
        stream.close()
        return found_hb, None
