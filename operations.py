"""独立 Tk 管理器的底层操作：检查、更新、装依赖、删插件。

只用标准库，不碰 UI（不调用 tkinter），不依赖 ComfyUI 核心。
日志里不打印代理凭据。
"""
import errno
import os
import stat
import sys
import shutil
import subprocess

# 版本下拉框的第一项，含义是“跟随当前分支远端最新版”。
LATEST = '最新版本 (Latest)'

# git 子进程超时（秒）。
_GIT_TIMEOUT = 90


class Operations:
    """底层操作集合，UI 层只调这里的方法。"""

    def __init__(self, git_exe, python_exe, proxy_url):
        # git / python 可执行文件路径，proxy_url 为空表示不走代理。
        self.git_exe = (git_exe or 'git').strip() or 'git'
        self.python_exe = (python_exe or '').strip()
        self.proxy_url = (proxy_url or '').strip()

    # ---------- 内部小工具 ----------

    def _redact(self, text):
        # 日志脱敏：把代理地址（可能带账号密码）替换掉。
        if not text:
            return text
        if self.proxy_url and self.proxy_url in text:
            return text.replace(self.proxy_url, '***')
        return text

    def _env(self):
        # git/pip 子进程环境：禁止交互，配好代理。
        env = os.environ.copy()
        env['GIT_TERMINAL_PROMPT'] = '0'
        env['GCM_INTERACTIVE'] = 'never'
        env['GIT_PAGER'] = 'cat'
        if self.proxy_url:
            env['http_proxy'] = self.proxy_url
            env['https_proxy'] = self.proxy_url
            env['HTTP_PROXY'] = self.proxy_url
            env['HTTPS_PROXY'] = self.proxy_url
        return env

    def _run_git(self, args, cwd):
        # 跑 git：列表传参防注入，隐藏窗口，超时 90 秒。
        cmd = [self.git_exe] + list(args)
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        try:
            p = subprocess.run(
                cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                startupinfo=startupinfo, env=self._env(), timeout=_GIT_TIMEOUT,
            )
            out = p.stdout.decode('utf-8', 'replace').strip()
            err = p.stderr.decode('utf-8', 'replace').strip()
            return p.returncode, self._redact(out), self._redact(err)
        except subprocess.TimeoutExpired:
            return -1, '', 'git 执行超时（90 秒）'
        except Exception as e:  # noqa: BLE001 - 路径不存在等情况要转成返回值
            return -1, '', self._redact(str(e))

    def _current_branch(self, path):
        # 返回分支名；游离 HEAD 时返回 None。
        code, out, _ = self._run_git(['symbolic-ref', '--short', '-q', 'HEAD'], path)
        if code == 0 and out.strip():
            return out.strip()
        return None

    def _default_origin_branch(self, path):
        # 取 origin 默认分支名，不猜 master，先本地后远端。
        code, out, _ = self._run_git(['symbolic-ref', 'refs/remotes/origin/HEAD'], path)
        if code == 0 and out.strip():
            # 形如 refs/remotes/origin/main
            ref = out.strip()
            name = ref.removeprefix('refs/remotes/origin/').strip()
            if name and name != 'HEAD':
                return name
        # 远端问一次：ls-remote --symref
        code, out, _ = self._run_git(['ls-remote', '--symref', 'origin', 'HEAD'], path)
        if code == 0:
            for line in out.splitlines():
                line = line.strip()
                if line.startswith('ref:'):
                    # 形如 ref: refs/heads/main\tHEAD
                    first = line.split()[1] if len(line.split()) > 1 else ''
                    name = first.removeprefix('refs/heads/').strip()
                    if name:
                        return name
        return None

    def _local_versions(self, path):
        # 本地 tag / commit 列表，第一项固定是 LATEST。
        versions = [LATEST]
        code, out, _ = self._run_git(['tag', '--sort=-creatordate'], path)
        if code == 0 and out:
            for t in out.splitlines()[:8]:
                t = t.strip()
                if t:
                    versions.append('Tag: %s' % t)
        code, out, _ = self._run_git(['log', '--pretty=format:%h - %s', '-n', '15'], path)
        if code == 0 and out:
            for c in out.splitlines():
                c = c.strip()
                if c:
                    versions.append('Commit: %s' % c)
        return versions

    def _current_commit(self, path):
        code, out, _ = self._run_git(
            ['log', '-1', '--format=%h - %s (%cd)', '--date=short'], path)
        if code == 0:
            return out.strip()
        return ''

    def _history_text(self, path, has_upstream):
        # 更新记录：待更新 + 最近已安装；无上游时只给最近历史。
        parts = []
        if has_upstream:
            code, out, _ = self._run_git(
                ['log', 'HEAD..@{u}', '--pretty=format:[%h] %s (%cd) - %an',
                 '--date=short'], path)
            if code == 0 and out.strip():
                n = out.strip().count('\n') + 1
                parts.append('=== 待更新内容（共 %d 条） ===\n%s' % (n, out.strip()))
            else:
                parts.append('远端暂无待更新内容。')
        code, out, _ = self._run_git(
            ['log', '-20', '--pretty=format:[%h] %s (%cd) - %an', '--date=short'], path)
        if code == 0 and out.strip():
            parts.append('=== 最近已安装的版本 ===\n%s' % out.strip())
        return '\n\n'.join(parts) if parts else '暂无版本记录'

    # ---------- 对外接口 ----------

    def inspect(self, path, fetch=False):
        """检查仓库状态，不弹 UI。

        fetch=True 会先 git fetch --prune 拉远端；
        fetch=False 只看本地远端记录，detail 里会注明。
        fetch 失败时状态为检查失败，不会误报最新。
        """
        path = os.path.abspath(os.path.normpath(path))
        req = os.path.join(path, 'requirements.txt')
        has_req = os.path.isfile(req)
        base = {'git': False, 'update': False, 'requirements': has_req,
                'versions': [], 'commit': '', 'history': '', 'detail': ''}

        if not os.path.isdir(path):
            base.update(status='目录不存在', detail='目录不存在：%s' % path)
            return base
        # 只认本目录下的 .git，防止父目录仓库误识别。
        if not os.path.lexists(os.path.join(path, '.git')):
            base.update(status='非Git仓库',
                        detail='该目录不是 Git 仓库（未找到 .git）。')
            return base
        base['git'] = True

        code, _, err = self._run_git(['rev-parse', '--verify', 'HEAD'], path)
        if code != 0:
            base.update(status='检查失败', detail='无法读取 Git 仓库：%s' % (err or '没有有效提交'))
            return base

        fetch_note = ''
        if fetch:
            code, _, err = self._run_git(['fetch', '--prune'], path)
            if code != 0:
                base['versions'] = self._local_versions(path)
                base['commit'] = self._current_commit(path)
                base['history'] = self._history_text(path, False)
                base.update(status='检查失败',
                            detail='拉取远端失败（fetch --prune）：%s' % (err or '未知错误'))
                return base
        else:
            fetch_note = '（仅本地远端记录，未联网拉取）'

        branch = self._current_branch(path)
        if branch is None:
            # 游离 HEAD：一般是之前切到了历史 tag / commit。
            base['versions'] = self._local_versions(path)
            base['commit'] = self._current_commit(path)
            base['history'] = self._history_text(path, False)
            base.update(status='游离HEAD（历史版本）', update=False,
                        detail='当前为游离 HEAD（历史版本）%s，选“%s”可回到默认分支再更新。'
                               % (fetch_note, LATEST))
            return base

        # 取上游分支，无上游则直接说明。
        code, upstream, _ = self._run_git(
            ['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'], path)
        if code != 0 or not upstream.strip():
            base['versions'] = self._local_versions(path)
            base['commit'] = self._current_commit(path)
            base['history'] = self._history_text(path, False)
            base.update(status='无上游分支', update=False,
                        detail='分支 %s 没有关联上游分支%s。' % (branch, fetch_note))
            return base
        upstream = upstream.strip()

        # machine-readable 的领先/落后计数：左=本地多，右=远端多。
        code, out, err = self._run_git(
            ['rev-list', '--left-right', '--count', 'HEAD...@{u}'], path)
        if code != 0:
            base['versions'] = self._local_versions(path)
            base['commit'] = self._current_commit(path)
            base['history'] = self._history_text(path, True)
            base.update(status='检查失败',
                        detail='读取远端计数失败：%s%s' % (err or '未知错误', fetch_note))
            return base
        try:
            ahead_s, behind_s = out.split()
            ahead, behind = int(ahead_s), int(behind_s)
        except (ValueError, AttributeError):
            base['versions'] = self._local_versions(path)
            base['commit'] = self._current_commit(path)
            base['history'] = self._history_text(path, True)
            base.update(status='检查失败', detail='远端计数解析失败%s。' % fetch_note)
            return base

        base['versions'] = self._local_versions(path)
        base['commit'] = self._current_commit(path)
        base['history'] = self._history_text(path, True)
        pos = '分支 %s，上游 %s，本地领先 %d、落后 %d%s' % (
            branch, upstream, ahead, behind, fetch_note)
        if behind > 0 and ahead == 0:
            base.update(status='有更新（落后远端）', update=True,
                        detail='远端有 %d 个新提交可更新。%s' % (behind, pos))
        elif ahead > 0 and behind == 0:
            base.update(status='本地领先（远端无新内容）', update=False,
                        detail='本地比远端多 %d 个提交，远端无新内容。%s' % (ahead, pos))
        elif ahead > 0 and behind > 0:
            base.update(status='本地与远端分叉', update=True,
                        detail='本地多 %d、远端多 %d，已分叉（pull --ff-only 可能失败）。%s'
                               % (ahead, behind, pos))
        else:
            base.update(status='最新版本' if fetch else '与本地远端记录一致',
                        update=False, detail='与远端记录一致。%s' % pos)
        return base

    def update(self, path, selection):
        """执行更新/回退，成功返回中文消息，失败抛 RuntimeError。"""
        path = os.path.abspath(os.path.normpath(path))
        if not os.path.lexists(os.path.join(path, '.git')):
            raise RuntimeError('不是 Git 仓库：%s' % path)
        sel = (selection or '').strip()
        if not sel:
            raise RuntimeError('未选择要执行的操作')

        if sel == LATEST:
            # 当前在游离 HEAD，先回到 origin 默认分支；失败立刻停。
            branch = self._current_branch(path)
            if branch is None:
                branch = self._default_origin_branch(path)
                if not branch:
                    raise RuntimeError('游离 HEAD 且找不到 origin 默认分支，无法更新')
                code, _, err = self._run_git(['checkout', branch, '--'], path)
                if code != 0:
                    raise RuntimeError('切回默认分支 %s 失败：%s' % (branch, err or '未知错误'))
            # 跟随当前分支的上游，只做快进合并。
            code, out, err = self._run_git(['pull', '--ff-only'], path)
            if code != 0:
                msg = err or out or '未知错误'
                raise RuntimeError(
                    '更新失败（本地修改已保留，未强制覆盖）：%s\n'
                    '请先提交或暂存本地修改后再试。' % msg)
            return '更新成功：已跟随远端最新版本。%s' % (out.strip()[:200] if out else '')

        target = ''
        if sel.startswith('Tag: '):
            target = 'refs/tags/' + sel[len('Tag: '):].strip()
        elif sel.startswith('Commit: '):
            rest = sel[len('Commit: '):].strip()
            # 形如 "hash - subject"，只取横杠前的 hash。
            target = rest.split(' - ', 1)[0].split()[0] if rest.split() else ''
        else:
            raise RuntimeError('无法识别的版本选项：%s' % sel)
        if not target or target.startswith('-'):
            raise RuntimeError('版本号不合法，已拒绝执行：%s' % sel)
        if '\n' in target or '\x00' in target:
            raise RuntimeError('版本号不合法，已拒绝执行')
        # 历史版本用 --detach 切过去，不动本地修改；失败立刻停。
        code, _, err = self._run_git(['checkout', '--detach', target, '--'], path)
        if code != 0:
            raise RuntimeError('切换到 %s 失败（本地修改已保留）：%s'
                               % (target, err or '未知错误'))
        return '已切换到历史版本：%s' % target

    def install_requirements(self, path):
        """新开可见控制台跑 pip，等待结束，成功返回消息，失败抛错。"""
        path = os.path.abspath(os.path.normpath(path))
        req = os.path.abspath(os.path.join(path, 'requirements.txt'))
        if not os.path.isfile(req):
            raise RuntimeError('未找到 requirements.txt：%s' % req)
        if not self.python_exe:
            raise RuntimeError('未配置 Python 路径')
        helper = os.path.abspath(__file__)
        # 用本文件的子入口跑 pip：不用 bat 拼接路径，列表传参防注入；
        # 临时产物不落仓库（本文件即脚本，无额外临时文件）。
        cmd = [self.python_exe, helper, '--pip-helper', req]
        flags = 0
        if os.name == 'nt':
            flags = 0x00000010  # CREATE_NEW_CONSOLE：新开可见窗口
        try:
            proc = subprocess.Popen(cmd, cwd=path, env=self._env(),
                                    creationflags=flags)
            proc.wait()
        except Exception as e:  # noqa: BLE001 - 路径错误等要转成中文报错
            raise RuntimeError('启动 pip 失败：%s' % self._redact(str(e)))
        if proc.returncode == 0:
            return '依赖安装完成'
        raise RuntimeError('pip 安装失败，返回码：%s（详细看新开窗口里的日志）'
                           % proc.returncode)

    def remove_plugin(self, path, nodes_path):
        """删除 custom_nodes 下的直接子项，不存在算幂等成功。"""
        target = os.path.abspath(os.path.normpath(path))
        base = os.path.abspath(os.path.normpath(nodes_path))
        # 只允许删 custom_nodes 的直接子项，防止误删别处。
        if os.path.dirname(target) != base:
            raise RuntimeError('仅允许删除 custom_nodes 下的直接子目录：%s' % path)
        if not os.path.lexists(target):
            return '插件已不存在，无需删除'

        def _is_link(p):
            if os.path.islink(p):
                return True
            if os.name == 'nt':
                try:
                    st = os.lstat(p)
                except OSError:
                    return False
                # Windows 目录 junction / symlink 都带 reparse 点。
                return bool(getattr(st, 'st_file_attributes', 0) & 0x400)
            return False

        try:
            if _is_link(target):
                # 只删链接本身，不动链接目标。
                try:
                    os.unlink(target)
                except OSError:
                    os.rmdir(target)
                return '已删除插件链接'
            if os.path.isdir(target) and not os.path.isfile(target):
                def _onerror(func, p, exc_info):
                    # 只在权限类错误时去只读重试，其它错误直接抛。
                    exc = exc_info[1]
                    if func not in (os.unlink, os.remove, os.rmdir):
                        raise exc
                    if not isinstance(exc, PermissionError) and (
                            not isinstance(exc, OSError) or exc.errno not in (errno.EACCES, errno.EPERM)):
                        raise exc
                    os.chmod(p, os.stat(p).st_mode | stat.S_IWRITE | stat.S_IREAD)
                    func(p)
                try:
                    shutil.rmtree(target, onerror=_onerror)
                except PermissionError as e:
                    raise RuntimeError(
                        '删除失败：%s\n原因：%s\n文件可能只读、权限不足或正在使用；'
                        '请关闭 ComfyUI 及占用该文件的程序后重试。'
                        % (e.filename or target, e)) from e
                except OSError as e:
                    raise RuntimeError(
                        '删除失败：%s\n原因：%s\n如文件被占用，请关闭 ComfyUI 后重试。'
                        % (target, e)) from e
                return '插件已删除'
            # 普通文件。
            os.remove(target)
            return '插件文件已删除'
        except RuntimeError:
            raise
        except OSError as e:
            raise RuntimeError(
                '删除失败：%s\n原因：%s\n如文件被占用，请关闭 ComfyUI 后重试。'
                % (target, e)) from e


def _pip_helper_main(req_path):
    """pip 子入口：嵌入版 python 无 tkinter 也能跑；保留真实返回码。"""
    req = os.path.abspath(req_path)
    print('============================================')
    print('  安装依赖：%s' % req)
    print('============================================')
    print()
    code = 1
    try:
        # 直接子进程跑 pip，日志全打到当前新窗口。
        proc = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '-r', req])
        code = proc.returncode
        print()
        if code == 0:
            print('============================================')
            print('  依赖安装完成！')
            print('============================================')
        else:
            print('============================================')
            print('  依赖安装出错，返回码：%s' % code)
            print('============================================')
    except Exception as e:  # noqa: BLE001 - pip 启动失败也要给用户看
        print('依赖安装出错：%s' % e)
        code = 1
    print()
    try:
        input('按回车关闭窗口…（窗口关闭不影响上面的安装结果）')
    except (EOFError, KeyboardInterrupt):
        pass
    sys.exit(code)


if __name__ == '__main__':
    # 子入口只认 --pip-helper，不影响 import 用法。
    if len(sys.argv) == 3 and sys.argv[1] == '--pip-helper':
        _pip_helper_main(sys.argv[2])
    else:
        print('用法：operations.py --pip-helper <requirements.txt绝对路径>')
        sys.exit(2)
