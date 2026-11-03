# SPDX-License-Identifier: Apache-2.0
# Adapted from fanyuantaier/wechatauto-replica; see NOTICE and LICENSES.
import logging
wxlog=logging.getLogger("wechat_readonly")
wxlog.addHandler(logging.NullHandler())
wxlog.propagate=False
