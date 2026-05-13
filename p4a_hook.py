import os

RECEIVER_XML = '''
        <receiver android:name=".BootReceiver"
                  android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.BOOT_COMPLETED"/>
                <category android:name="android.intent.category.DEFAULT"/>
            </intent-filter>
        </receiver>'''


def after_apk_build(ctx):
    for candidate in ('src/main/AndroidManifest.xml', 'AndroidManifest.xml'):
        if os.path.exists(candidate):
            manifest_path = candidate
            break
    else:
        print('[hook] AndroidManifest.xml not found, skipping BootReceiver injection')
        return

    with open(manifest_path, 'r', encoding='utf-8') as f:
        content = f.read()

    if 'BootReceiver' in content:
        print('[hook] BootReceiver already present, skipping')
        return

    content = content.replace('</application>', RECEIVER_XML + '\n    </application>', 1)

    with open(manifest_path, 'w', encoding='utf-8') as f:
        f.write(content)

    print(f'[hook] Injected BootReceiver into {manifest_path}')
