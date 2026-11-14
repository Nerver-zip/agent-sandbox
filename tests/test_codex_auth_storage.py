"""Filesystem and lifecycle regressions for Codex's persistent auth store."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
DOCKER_IMAGE = os.environ.get("CODEX_AUTH_TEST_DOCKER_IMAGE", "node:24-bookworm")


class CodexWrapperLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / ".codex"
        self.store = self.root / ".codex-auth"
        self.home.mkdir(mode=0o700)
        self.store.mkdir(mode=0o700)
        self.auth = self.store / "auth.json"
        self.lock = self.store / "auth.lock"
        self.link = self.home / "auth.json"
        self.lock_link = self.home / "auth.lock"
        self.marker = self.store / "auth-operation"
        self.auth.write_text('{"tokens":{"refresh_token":"fixture-old"}}\n')
        self.auth.chmod(0o600)
        self.lock.touch(mode=0o600)
        self.lock_link.symlink_to(self.lock)
        self.link.symlink_to(self.auth)
        self.real_codex = self.root / "codex-real"
        self.real_codex.write_text(textwrap.dedent("""\
            #!/usr/bin/env bash
            set -euo pipefail
            case "${1:-}" in
              login)
                rm -f -- "$CODEX_HOME/auth.json"
                if [[ "${CODEX_FAKE_RESULT:-ok}" == fail ]]; then exit 23; fi
                printf '{"tokens":{"refresh_token":"fixture-login"}}\\n' > "$CODEX_HOME/auth.json"
                chmod 0600 "$CODEX_HOME/auth.json"
                ;;
              logout)
                rm -f -- "$CODEX_HOME/auth.json"
                ;;
            esac
        """))
        self.real_codex.chmod(0o700)
        self.env = os.environ.copy()
        self.env.update({
            "HOME": str(self.root),
            "CODEX_HOME": str(self.home),
            "CODEX_AUTH_DIR": str(self.store),
            "CODEX_REAL_BIN": str(self.real_codex),
        })

    def tearDown(self):
        self.temp.cleanup()

    def run_wrapper(self, *args, **extra_env):
        env = self.env.copy()
        env.update(extra_env)
        return subprocess.run(
            ["bash", str(ROOT / "scripts/codex-wrapper.sh"), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
            check=False,
        )

    def assert_auth(self, expected):
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(os.readlink(self.link), str(self.auth))
        self.assertEqual(json.loads(self.auth.read_text()), expected)
        self.assertEqual(self.auth.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.marker.exists())
        self.assertEqual(sorted(p.name for p in self.store.iterdir()), ["auth.json", "auth.lock"])

    def assert_unauthenticated(self):
        # A bare {} auth.json makes Codex claim a ChatGPT login without a plan type,
        # so the logged-out state must have no auth.json visible in CODEX_HOME.
        self.assertFalse(os.path.lexists(self.link))
        self.assertEqual(json.loads(self.auth.read_text()), {})
        self.assertEqual(self.auth.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.marker.exists())
        self.assertEqual(sorted(p.name for p in self.store.iterdir()), ["auth.json", "auth.lock"])

    def test_device_login_unlinks_then_persists_new_credentials(self):
        result = self.run_wrapper("login", "--device-auth")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_auth({"tokens": {"refresh_token": "fixture-login"}})

    def test_logout_and_failed_login_leave_no_auth_file_visible_to_codex(self):
        logged_out = self.run_wrapper("logout")
        self.assertEqual(logged_out.returncode, 0, logged_out.stderr)
        self.assert_unauthenticated()

        failed_login = self.run_wrapper("login", "--device-auth", CODEX_FAKE_RESULT="fail")
        self.assertEqual(failed_login.returncode, 23)
        self.assert_unauthenticated()

    def test_login_after_logout_restores_link(self):
        self.assertEqual(self.run_wrapper("logout").returncode, 0)
        self.assert_unauthenticated()
        result = self.run_wrapper("login", "--device-auth")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_auth({"tokens": {"refresh_token": "fixture-login"}})

    def test_legacy_placeholder_link_is_removed_when_unauthenticated(self):
        self.auth.write_text("{}\n")
        self.assertTrue(self.link.is_symlink())
        result = self.run_wrapper("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_unauthenticated()


class CodexDockerStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker"):
            raise unittest.SkipTest("Docker is not installed")
        if subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            raise unittest.SkipTest("Docker daemon is unavailable")
        info = subprocess.run(["docker", "info", "--format", "{{json .SecurityOptions}}"], capture_output=True, text=True)
        if "rootless" in info.stdout.lower():
            raise unittest.SkipTest("Docker rootless mode cannot model UID 10001 bind-mount ownership")
        if subprocess.run(["docker", "image", "inspect", DOCKER_IMAGE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            raise unittest.SkipTest(f"Docker test image {DOCKER_IMAGE} is not present locally")

    def run_docker(self, args, *, input_text=None, timeout=60):
        return subprocess.run(
            ["docker", "run", "--rm", "--network", "none", *args],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def restore_host_permissions(self, store):
        code = (
            "const fs=require('node:fs'),p='/store';"
            f"for(const n of fs.readdirSync(p))fs.chownSync(p+'/'+n,{os.getuid()},{os.getgid()});"
            f"fs.chownSync(p,{os.getuid()},{os.getgid()});fs.chmodSync(p,0o700);"
        )
        subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "--mount", f"type=bind,src={store},dst=/store", DOCKER_IMAGE, "node", "-e", code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )

    def test_previous_single_file_bind_mount_reproduces_ebusy(self):
        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory) / "auth.json"
            auth.write_text('{"tokens":{"refresh_token":"fixture-old"}}\n')
            writer = textwrap.dedent("""\
                const fs=require('node:fs'), path=require('node:path');
                const target='/auth.json', temp=path.join(path.dirname(target),'.auth-'+process.pid);
                const fd=fs.openSync(temp,'wx',0o600);
                fs.writeFileSync(fd,'{"tokens":{"refresh_token":"fixture-new"}}\\n');
                fs.fsyncSync(fd); fs.closeSync(fd);
                try { fs.renameSync(temp,target); }
                catch (error) { try { fs.unlinkSync(temp); } catch (_) {} console.error(error.code); process.exitCode=1; }
            """)
            result = self.run_docker([
                "--mount", f"type=bind,src={auth},dst=/auth.json",
                DOCKER_IMAGE, "node", "-e", writer,
            ])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("EBUSY", result.stderr)
            self.assertEqual(auth.read_text(), '{"tokens":{"refresh_token":"fixture-old"}}\n')

    def test_directory_mount_symlink_atomic_replace_and_container_recreation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "codex"
            store.mkdir()
            (store / "auth.json").write_text('{"tokens":{"refresh_token":"fixture-initial"}}\n')
            (store / "auth.lock").touch(mode=0o600)
            code = textwrap.dedent("""\
                const fs=require('node:fs'), path=require('node:path');
                const store='/home/agent/.codex-auth', home=process.env.CODEX_HOME;
                const auth=store+'/auth.json', lock=store+'/auth.lock';
                fs.mkdirSync(home,{recursive:true,mode:0o700});
                fs.chownSync(home,10001,10001); fs.chmodSync(home,0o700);
                for (const file of [auth,lock]) { fs.chownSync(file,10001,10001); fs.chmodSync(file,0o600); }
                fs.chownSync(store,10001,10001); fs.chmodSync(store,0o700);
                process.setgid(10001); process.setuid(10001);
                fs.symlinkSync(auth,home+'/auth.json');
                fs.symlinkSync(lock,home+'/auth.lock');
                let resolved=home+'/auth.json';
                for(let i=0;i<40;i++) { const st=fs.lstatSync(resolved); if(!st.isSymbolicLink()) break; const next=fs.readlinkSync(resolved); resolved=path.isAbsolute(next)?next:path.resolve(path.dirname(resolved),next); }
                const temp=path.join(path.dirname(resolved),'.auth-'+process.pid);
                const fd=fs.openSync(temp,'wx',0o600);
                fs.writeFileSync(fd,'{"tokens":{"refresh_token":"fixture-rotated"}}\\n');
                fs.fsyncSync(fd); fs.closeSync(fd); fs.renameSync(temp,resolved);
                const info=fs.statSync(resolved);
                if(info.uid!==10001 || info.gid!==10001 || (info.mode&0o777)!==0o600) throw Error('auth file ownership or mode mismatch');
                if(!fs.lstatSync(home+'/auth.json').isSymbolicLink()) throw Error('auth symlink was replaced');
                if(fs.readFileSync(home+'/auth.json','utf8').indexOf('fixture-rotated')<0) throw Error('rotated document is unreadable through CODEX_HOME');
                if(fs.readdirSync(store).sort().join(',')!=='auth.json,auth.lock') throw Error('unexpected credential directory entry');
                console.log('atomic write passed');
            """)
            try:
                result = self.run_docker([
                    "-i", "--mount", f"type=bind,src={store},dst=/home/agent/.codex-auth",
                    "-e", "CODEX_HOME=/home/agent/.codex",
                    "-e", "CODEX_AUTH_DIR=/home/agent/.codex-auth",
                    DOCKER_IMAGE, "node", "-",
                ], input_text=code)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("atomic write passed", result.stdout)

                recreated = self.run_docker([
                    "--mount", f"type=bind,src={store},dst=/home/agent/.codex-auth",
                    DOCKER_IMAGE, "node", "-e",
                    "const fs=require('node:fs'),p='/home/agent/.codex-auth/auth.json',s=fs.statSync(p),v=JSON.parse(fs.readFileSync(p));if(s.uid!==10001||s.gid!==10001||(s.mode&0o777)!==0o600||v.tokens.refresh_token!=='fixture-rotated')process.exit(1);console.log('recreation passed')",
                ])
                self.assertEqual(recreated.returncode, 0, recreated.stderr)
                self.assertIn("recreation passed", recreated.stdout)
            finally:
                self.restore_host_permissions(store)

    def test_import_and_reset_scripts_preserve_permissions_and_refuse_running_container(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            scripts = fixture / "scripts"
            codex_dir = fixture / "persistent" / "codex"
            fake_bin = fixture / "bin"
            scripts.mkdir()
            codex_dir.mkdir(parents=True)
            fake_bin.mkdir()
            for name in ("import-codex-auth.sh", "reset-codex-auth.sh"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)
                if not (scripts / name).stat().st_mode & 0o111:
                    raise AssertionError(f"{name} must remain directly executable")
            (fixture / "source.json").write_text('{"tokens":{"refresh_token":"fixture-import"}}\n')
            (codex_dir / "auth.json").write_text('{"tokens":{"refresh_token":"fixture-old"}}\n')
            (codex_dir / "auth.lock").touch(mode=0o600)
            (fake_bin / "docker").write_text(textwrap.dedent("""\
                #!/bin/sh
                if [ "${1:-}" = ps ] && [ "${FAKE_WORKSPACE_RUNNING:-0}" = 1 ]; then
                  printf 'fixture-container\\n'
                fi
                exit 0
            """))
            (fake_bin / "docker").chmod(0o755)
            driver = fixture / "run.sh"
            driver.write_text(textwrap.dedent("""\
                #!/usr/bin/env bash
                set -euo pipefail
                export PATH=/fixture/bin:$PATH
                chown -R 10001:10001 /fixture/persistent/codex
                chmod 0700 /fixture/persistent/codex
                chmod 0600 /fixture/persistent/codex/auth.json /fixture/persistent/codex/auth.lock
                /fixture/scripts/import-codex-auth.sh /fixture/source.json
                python3 - <<'PYVERIFY'
                import json, os, stat
                from pathlib import Path
                p=Path('/fixture/persistent/codex/auth.json')
                assert json.loads(p.read_text())['tokens']['refresh_token']=='fixture-import'
                assert p.stat().st_uid==10001 and stat.S_IMODE(p.stat().st_mode)==0o600
                assert (p.parent/'auth-operation').read_text()=='import\\n'
                assert sorted(x.name for x in p.parent.iterdir())==['auth-operation','auth.json','auth.lock']
                PYVERIFY
                printf '{"tokens":{"refresh_token":"fixture-preserve"}}\\n' > /fixture/persistent/codex/auth.json
                chmod 0600 /fixture/persistent/codex/auth.json
                if FAKE_WORKSPACE_RUNNING=1 /fixture/scripts/reset-codex-auth.sh; then exit 31; fi
                python3 -c "import json; assert json.load(open('/fixture/persistent/codex/auth.json'))['tokens']['refresh_token']=='fixture-preserve'"
                if FAKE_WORKSPACE_RUNNING=1 /fixture/scripts/import-codex-auth.sh /fixture/source.json; then exit 32; fi
                python3 -c "import json; assert json.load(open('/fixture/persistent/codex/auth.json'))['tokens']['refresh_token']=='fixture-preserve'"
                /fixture/scripts/reset-codex-auth.sh
                python3 - <<'PYVERIFY'
                import json, os, stat
                from pathlib import Path
                p=Path('/fixture/persistent/codex/auth.json')
                assert json.loads(p.read_text())=={}
                assert p.stat().st_uid==10001 and stat.S_IMODE(p.stat().st_mode)==0o600
                assert (p.parent/'auth-operation').read_text()=='logout\\n'
                assert sorted(x.name for x in p.parent.iterdir())==['auth-operation','auth.json','auth.lock']
                PYVERIFY
            """))
            driver.chmod(0o700)
            try:
                result = self.run_docker([
                    "--mount", f"type=bind,src={fixture},dst=/fixture",
                    DOCKER_IMAGE, "bash", "/fixture/run.sh",
                ])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Imported Codex authentication", result.stdout)
                self.assertIn("Persisted Codex authentication reset", result.stdout)
            finally:
                self.restore_host_permissions(codex_dir)


if __name__ == "__main__":
    unittest.main()
