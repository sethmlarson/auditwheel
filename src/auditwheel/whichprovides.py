"""
Module which abstracts support for 'yum whichprovides' to many package managers
"""

from __future__ import annotations

import dataclasses
import re
import shutil
import subprocess
import typing

__all__ = ["whichprovides"]

_PACKAGE_MANAGER_BINS: dict[str, str | typing.Literal[False]] = {}
_OS_RELEASE_LINES_RE = re.compile(r"^([A-Z_]+)=(?:\"([^\"]*)\"|(.*))$", re.MULTILINE)
_APK_WHO_OWNS_RE = re.compile(r" is owned by ([^\s\-]+)-([^\s]+)\Z", re.MULTILINE)
_DPKG_SEARCH_RE = re.compile(r"^([^:]+):")
_DPKG_VERSION_RE = re.compile(r"^Version: ([^\s]+)", re.MULTILINE)
_APT_FILE_SEARCH_RE = re.compile(r"^([^:]+): ")


@dataclasses.dataclass
class Provides:
    package_type: str
    distro: str | None
    package_name: str
    package_version: str

    @property
    def purl(self) -> str:
        return (
            f"pkg:{self.package_type}/{self.distro + '/' if self.distro else ''}"
            f"{self.package_name}@{self.package_version}" + ("?distro=almalinux-8")
        )


def _os_release() -> dict[str, str]:
    """Dumb method of finding os-release information"""
    try:
        with open("/etc/os-release") as f:
            os_release = {}
            for name, value_quoted, value_unquoted in _OS_RELEASE_LINES_RE.findall(
                f.read()
            ):
                value = value_quoted if value_quoted else value_unquoted
                os_release[name] = value
            return os_release
    except OSError:
        return {}


def _package_manager_bin(
    binaryname: str, *, expect_returncodes: None | set[int] = None
) -> str | None:
    """Try to find a valid binary for package managers"""
    has_bin = _PACKAGE_MANAGER_BINS.get(binaryname)
    assert has_bin is not True
    if has_bin is False:
        return None
    elif has_bin is not None:
        return has_bin
    bin_which = shutil.which(binaryname)
    if bin_which is None:  # Cache the 'not-found' result.
        _PACKAGE_MANAGER_BINS[binaryname] = False
        return None
    try:
        subprocess.check_call(
            [bin_which, "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _PACKAGE_MANAGER_BINS[binaryname] = bin_which
        return bin_which
    except subprocess.CalledProcessError as e:
        # If running --version returns an non-zero exit we
        # explicitly allow that here.
        if expect_returncodes and e.returncode in expect_returncodes:
            _PACKAGE_MANAGER_BINS[binaryname] = bin_which
            return bin_which
        _PACKAGE_MANAGER_BINS[binaryname] = False
    return None


def whichprovides(filepath: str) -> Provides | None:
    """Return a package information for the package that provides a file"""
    distro = _os_release().get("ID", None)

    # apk (Alpine)
    if distro and (apk_bin := _package_manager_bin("apk")):
        try:
            # $ apk info --who-owns /bin/bash
            # /bin/bash is owned by bash-5.2.26-r0
            stdout = subprocess.check_output(
                [apk_bin, "info", "--who-owns", filepath],
                stderr=subprocess.DEVNULL,
            ).decode()
            if match := _APK_WHO_OWNS_RE.search(stdout):
                package_name = match.group(1)
                package_version = match.group(2)
                return Provides(
                    package_type="apk",
                    distro=distro,
                    package_name=package_name,
                    package_version=package_version,
                )
        except subprocess.CalledProcessError:
            pass

    # dpkg (Debian, Ubuntu)
    if distro and (dpkg_bin := _package_manager_bin("dpkg")):
        try:
            # $ dpkg -S /bin/bash
            # bash: /bin/bash
            stdout = subprocess.check_output(
                [dpkg_bin, "-S", filepath],
                stderr=subprocess.DEVNULL,
            ).decode()
            if match := _DPKG_SEARCH_RE.search(stdout):
                package_name = match.group(1)
                # $ dpkg -s bash
                # ...
                # Version: 5.1-6ubuntu1.1
                stdout = subprocess.check_output(
                    [dpkg_bin, "-s", package_name],
                    stderr=subprocess.DEVNULL,
                ).decode()
                if match := _DPKG_VERSION_RE.search(stdout):
                    package_version = match.group(1)
                    return Provides(
                        package_type="deb",
                        distro=distro,
                        package_name=package_name,
                        package_version=package_version,
                    )
        except subprocess.CalledProcessError:
            pass

    # rpm (CentOS, Red Hat, AlmaLinux, Rocky Linux)
    if distro and (rpm_bin := _package_manager_bin("rpm")):
        try:
            # $ rpm -qf --queryformat "%{NAME} %{VERSION} %{RELEASE} ${ARCH}" /bin/bash
            # bash 4.4.20 4.el8_6
            stdout = subprocess.check_output(
                [
                    rpm_bin,
                    "-qf",
                    "--queryformat",
                    "%{NAME} %{VERSION} %{RELEASE} %{ARCH}",
                    filepath,
                ],
                stderr=subprocess.DEVNULL,
            ).decode()
            print(repr(stdout))
            package_name, package_version, package_release, *_ = stdout.strip().split(
                " ", 4
            )
            return Provides(
                package_type="rpm",
                distro=distro,
                package_name=package_name,
                package_version=f"{package_version}-{package_release}",
            )
        except subprocess.CalledProcessError:
            pass

    # apt (Ubuntu, slower than Debian)
    if (
        distro
        and (apt_bin := _package_manager_bin("apt"))
        and (apt_file_bin := _package_manager_bin("apt-file", expect_returncodes={2}))
    ):
        try:
            # $ apt-file search <path>
            # apt-file search <path>
            # Finding relevant cache files to search ...
            # ...
            # libwebpdemux2: /usr/lib/x86_64-linux-gnu/libwebpdemux.so.2.0.9
            stdout = subprocess.check_output(
                [apt_file_bin, "search", filepath],
                stderr=subprocess.DEVNULL,
            ).decode()
            if match := _APT_FILE_SEARCH_RE.search(stdout):
                package_name = match.group(1)
                stdout = subprocess.check_output(
                    [apt_bin, "show", package_name],
                    stderr=subprocess.DEVNULL,
                ).decode()
                if match := _DPKG_VERSION_RE.search(stdout):
                    package_version = match.group(1)
                    return Provides(
                        package_type="deb",
                        distro=distro,
                        package_name=package_name,
                        package_version=package_version,
                    )
        except subprocess.CalledProcessError:
            pass

    return None
