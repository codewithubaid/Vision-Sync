"""One-time, guarded cleanup of the confirmed VisionSync mother-name duplicates.

Place next to vision_memory.db and execute with your VisionSync venv Python.
This script does not create a backup, alter profile names, or renumber IDs.
"""
from pathlib import Path
import sqlite3
import sys

DATABASE = Path(__file__).resolve().parent / 'vision_memory.db'
EXPECTED = {
    13: ('profilemothername', 'Teena', 'profile'),
    12: ('teena', 'Teena', 'profilemothername'),
    7: ('mothers name', "My mother's name is Teena", 'memory'),
    1: ('profileusername', 'Your name is Zaema', 'profile'),
}


def main():
    if not DATABASE.is_file():
        print(f'ERROR: No VisionSync database at {DATABASE}. Place this script in the VisionSync project folder.')
        return 1

    connection = sqlite3.connect(str(DATABASE), timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('BEGIN IMMEDIATE')
        found = {
            int(row['id']): (row['key'], row['information'], row['category'])
            for row in connection.execute(
                'SELECT id, key, information, category FROM memory_items WHERE id IN (1, 7, 12, 13)'
            )
        }
        if found.get(13) != EXPECTED[13] or found.get(1) != EXPECTED[1]:
            print('STOPPED: Canonical mother/name records have changed. Nothing was deleted.')
            print('Current records:', found)
            connection.rollback()
            return 2
        duplicates = []
        for duplicate_id in (12, 7):
            if duplicate_id not in found:
                continue  # already removed, safe to run again
            if found[duplicate_id] != EXPECTED[duplicate_id]:
                print(f'STOPPED: Memory {duplicate_id} has changed. Nothing was deleted.')
                print('Current record:', found[duplicate_id])
                connection.rollback()
                return 2
            duplicates.append(duplicate_id)

        if not duplicates:
            connection.rollback()
            print('Already clean: canonical mother-name and user-name memories are intact.')
            return 0

        for duplicate_id in duplicates:
            cursor = connection.execute('DELETE FROM memory_items WHERE id = ?', (duplicate_id,))
            if cursor.rowcount != 1:
                raise RuntimeError(f'Deletion was not verified for ID {duplicate_id}')

        remaining = {
            int(row['id']): (row['key'], row['information'], row['category'])
            for row in connection.execute(
                'SELECT id, key, information, category FROM memory_items WHERE id IN (1, 7, 12, 13)'
            )
        }
        if (remaining.get(13) != EXPECTED[13]
                or remaining.get(1) != EXPECTED[1]
                or any(duplicate_id in remaining for duplicate_id in duplicates)):
            raise RuntimeError('Post-delete verification failed')
        connection.commit()
        print('SUCCESS: Removed duplicate memory IDs:', ', '.join(map(str, duplicates)))
        print('KEPT: Mother\'s name = Teena (internal ID 13)')
        print('KEPT: Your name = Zaema (internal ID 1)')
        print('Other memory records were not modified.')
        return 0
    except (sqlite3.Error, RuntimeError) as error:
        connection.rollback()
        print('STOPPED: Transaction rolled back. Reason:', error)
        return 1
    finally:
        connection.close()


if __name__ == '__main__':
    sys.exit(main())
