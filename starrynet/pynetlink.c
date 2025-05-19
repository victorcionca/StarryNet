// pynetlink.c - Direct netlink interface for traffic control
#include <Python.h>
// POSIX and Linux
#include <unistd.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <linux/netlink.h>
#include <linux/rtnetlink.h>
#include <linux/pkt_sched.h>
#include <linux/if_link.h>
#include <arpa/inet.h>
#include <net/if.h>
// std C
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include <time.h>


// Update netem qdisc using netlink
static int update_netem_(
    const char *if_name, uint32_t delay_ms, uint32_t loss_percent, 
    const char *rate_str, char *err_str, size_t max_len) 
{
    // Get interface index
    unsigned int if_idx = if_nametoindex(if_name);
    if (if_idx == 0) {
        snprintf(err_str, max_len, "Interface not found: %s", if_name);
        return -1;
    }

    // Parse rate string (e.g., "10Gbit" to bps)
    double rate_value = 0.0;
    char rate_unit[16] = {0};
    uint64_t rate_bps = 0;

    if (sscanf(rate_str, "%lf%15s", &rate_value, rate_unit) == 2) {
        if (strcmp(rate_unit, "Gbit") == 0) {
            rate_bps = (uint64_t)(rate_value * 1000000000 / 8);
        } else if (strcmp(rate_unit, "Mbit") == 0) {
            rate_bps = (uint64_t)(rate_value * 1000000 / 8);
        } else if (strcmp(rate_unit, "Kbit") == 0) {
            rate_bps = (uint64_t)(rate_value * 1000 / 8);
        } else {
            // Default to bps
            rate_bps = (uint64_t)rate_value;
        }
    } else {
        // Try to parse as just a number (bps)
        if (sscanf(rate_str, "%lf", &rate_value) == 1) {
            rate_bps = (uint64_t)rate_value * 1000000000 / 8;
        }
    }

    // Create netlink socket
    int sock_fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE);
    if (sock_fd < 0) {
        snprintf(err_str, max_len, "Failed to open netlink socket: %s", strerror(errno));
        return -1;
    }
    
    // Bind the socket to a random dynamic port
    struct sockaddr_nl sa = {
        .nl_family = AF_NETLINK,
        .nl_pid = 0,  // Let kernel assign a unique PID
        .nl_groups = 0
    };
    
    if (bind(sock_fd, (struct sockaddr*)&sa, sizeof(sa)) < 0) {
        snprintf(err_str, max_len, "Failed to bind netlink socket: %s", strerror(errno));
        close(sock_fd);
        return -1;
    }

    // Get our port number
    socklen_t addr_len = sizeof(sa);
    if (getsockname(sock_fd, (struct sockaddr*)&sa, &addr_len) < 0) {
        snprintf(err_str, max_len, "Failed to get socket name: %s", strerror(errno));
        close(sock_fd);
        return -1;
    }
    
    // Generate a unique sequence number based on time
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    uint32_t seq = ts.tv_sec * 1000 + ts.tv_nsec / 1000000;  // Use milliseconds as sequence

    // netlink msg buffer
    uint8_t buf[1024];
    struct nlmsghdr* nl_hdr = (struct nlmsghdr*)buf;
    nl_hdr->nlmsg_len = NLMSG_LENGTH(sizeof(struct tcmsg));
    nl_hdr->nlmsg_type = RTM_NEWQDISC;
    nl_hdr->nlmsg_seq = seq;
    
    // For existing qdisc change, not add - avoid "File exists" error
    nl_hdr->nlmsg_flags = NLM_F_REQUEST | NLM_F_ACK | NLM_F_CREATE  | NLM_F_REPLACE; 
    
    // Use our assigned PID
    nl_hdr->nlmsg_pid = sa.nl_pid;

    // TC message structure
    struct tcmsg* tc_msg = NLMSG_DATA(nl_hdr);
    tc_msg->tcm_family = AF_UNSPEC;
    tc_msg->tcm__pad1 = tc_msg->tcm__pad2 = 0;
    tc_msg->tcm_ifindex = if_idx;
    
    tc_msg->tcm_handle = 0;
    tc_msg->tcm_parent = TC_H_ROOT;  // Parent is ROOT
    tc_msg->tcm_info = 0;

    // Add qdisc kind attribute (netem)
    struct rtattr* rta = (struct rtattr*)((char*)nl_hdr + NLMSG_ALIGN(nl_hdr->nlmsg_len));
    rta->rta_type = TCA_KIND;
    rta->rta_len = RTA_LENGTH(strlen("netem") + 1);
    memcpy(RTA_DATA(rta), "netem", strlen("netem") + 1);
    nl_hdr->nlmsg_len = NLMSG_ALIGN(nl_hdr->nlmsg_len) + RTA_ALIGN(rta->rta_len);

    // Start with options
    struct rtattr* opts = (struct rtattr*)((char*)nl_hdr + NLMSG_ALIGN(nl_hdr->nlmsg_len));
    opts->rta_type = TCA_OPTIONS;
    opts->rta_len = RTA_LENGTH(sizeof(struct tc_netem_qopt));
    
    // Set the netem parameters
    struct tc_netem_qopt* qopt = (struct tc_netem_qopt*)RTA_DATA(opts);
    memset(qopt, 0, sizeof(*qopt));
    qopt->limit = 1000;
    qopt->latency = delay_ms * 1000;  // Convert ms to us
    qopt->loss = loss_percent * 10000 / 100;  // Convert % to ppm
    
    // Update total length
    nl_hdr->nlmsg_len = NLMSG_ALIGN(nl_hdr->nlmsg_len) + RTA_ALIGN(opts->rta_len);
    
    // If we have a rate specified, add rate information
    if (rate_bps > 0) {
        struct rtattr* rate_attr = (struct rtattr*)((char*)opts + RTA_ALIGN(opts->rta_len));
        rate_attr->rta_type = TCA_NETEM_RATE;
        rate_attr->rta_len = RTA_LENGTH(sizeof(struct tc_netem_rate));
        
        struct tc_netem_rate* rate = (struct tc_netem_rate*)RTA_DATA(rate_attr);
        memset(rate, 0, sizeof(*rate));
        rate->rate = (rate_bps < (1ULL << 32)) ? rate_bps : ~0U;
        
        // Update options length
        opts->rta_len += RTA_ALIGN(rate_attr->rta_len);
        nl_hdr->nlmsg_len += RTA_ALIGN(rate_attr->rta_len);
        
        // Add 64-bit value if needed
        if (rate_bps >= (1ULL << 32)) {
            struct rtattr* rate64 = (struct rtattr*)((char*)rate_attr + RTA_ALIGN(rate_attr->rta_len));
            rate64->rta_type = TCA_NETEM_RATE64;
            rate64->rta_len = RTA_LENGTH(sizeof(uint64_t));
            *(uint64_t*)RTA_DATA(rate64) = rate_bps;
            
            // Update lengths
            opts->rta_len += RTA_ALIGN(rate64->rta_len);
            nl_hdr->nlmsg_len += RTA_ALIGN(rate64->rta_len);
        }
    }

    // Send message
    struct sockaddr_nl dst = {
        .nl_family = AF_NETLINK,
        .nl_pid = 0,  // To kernel
        .nl_groups = 0
    };
    
    struct iovec iov = {
        .iov_base = nl_hdr,
        .iov_len = nl_hdr->nlmsg_len
    };
    
    struct msghdr msg = {
        .msg_name = &dst,
        .msg_namelen = sizeof(dst),
        .msg_iov = &iov,
        .msg_iovlen = 1
    };
    
    if (sendmsg(sock_fd, &msg, 0) < 0) {
        snprintf(err_str, max_len, "Failed to send netlink message: %s", strerror(errno));
        close(sock_fd);
        return -1;
    }

    // Receive response
    char resp[1024];
    iov.iov_base = resp;
    iov.iov_len = sizeof(resp);
    
    int ret = recvmsg(sock_fd, &msg, 0);
    if (ret < 0) {
        snprintf(err_str, max_len, "Failed to receive netlink response: %s", strerror(errno));
        close(sock_fd);
        return -1;
    }

    // Check for errors
    struct nlmsghdr* resp_hdr = (struct nlmsghdr*)resp;
    if (resp_hdr->nlmsg_type == NLMSG_ERROR) {
        struct nlmsgerr* err = (struct nlmsgerr*)NLMSG_DATA(resp_hdr);
        if (err->error) {
            snprintf(err_str, max_len, "Netlink error: %s (%d)", strerror(-err->error), -err->error);
            close(sock_fd);
            return -1;
        }
    }
    
    close(sock_fd);
    return 0;
}

// Delete a network interface using netlink (replaces ip link del)
static int del_link_(const char *if_name, char *err_str, size_t max_len) {
    // Get interface index
    unsigned int if_idx = if_nametoindex(if_name);
    if (if_idx == 0) {
        snprintf(err_str, max_len, "Interface not found: %s", if_name);
        return -1;
    }

    // Create netlink socket
    int sock_fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE);
    if (sock_fd < 0) {
        snprintf(err_str, max_len, "Failed to open netlink socket: %s", strerror(errno));
        return -1;
    }
    
    // Bind the socket
    struct sockaddr_nl sa = {
        .nl_family = AF_NETLINK,
        .nl_pid = 0,  // Let kernel assign a unique PID
        .nl_groups = 0
    };
    
    if (bind(sock_fd, (struct sockaddr*)&sa, sizeof(sa)) < 0) {
        snprintf(err_str, max_len, "Failed to bind netlink socket: %s", strerror(errno));
        close(sock_fd);
        return -1;
    }

    // Get our port number
    socklen_t addr_len = sizeof(sa);
    if (getsockname(sock_fd, (struct sockaddr*)&sa, &addr_len) < 0) {
        snprintf(err_str, max_len, "Failed to get socket name: %s", strerror(errno));
        close(sock_fd);
        return -1;
    }
    
    // Generate sequence number
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    uint32_t seq = ts.tv_sec * 1000 + ts.tv_nsec / 1000000;

    // Prepare netlink message for deleting the interface
    uint8_t buf[512] = {0};
    struct nlmsghdr* nl_hdr = (struct nlmsghdr*)buf;
    nl_hdr->nlmsg_len = NLMSG_LENGTH(sizeof(struct ifinfomsg));
    nl_hdr->nlmsg_type = RTM_DELLINK;  // Delete link
    nl_hdr->nlmsg_flags = NLM_F_REQUEST | NLM_F_ACK;
    nl_hdr->nlmsg_seq = seq;
    nl_hdr->nlmsg_pid = sa.nl_pid;

    // Interface info message
    struct ifinfomsg* if_msg = NLMSG_DATA(nl_hdr);
    memset(if_msg, 0, sizeof(struct ifinfomsg));
    if_msg->ifi_family = AF_UNSPEC;
    if_msg->ifi_index = if_idx;  // Specify interface by index

    // Send message
    struct sockaddr_nl dst = {
        .nl_family = AF_NETLINK,
        .nl_pid = 0,  // To kernel
        .nl_groups = 0
    };
    
    struct iovec iov = {
        .iov_base = nl_hdr,
        .iov_len = nl_hdr->nlmsg_len
    };
    
    struct msghdr msg = {
        .msg_name = &dst,
        .msg_namelen = sizeof(dst),
        .msg_iov = &iov,
        .msg_iovlen = 1
    };
    
    if (sendmsg(sock_fd, &msg, 0) < 0) {
        snprintf(err_str, max_len, "Failed to send netlink message: %s", strerror(errno));
        close(sock_fd);
        return -1;
    }

    // Receive response
    char resp[512];
    iov.iov_base = resp;
    iov.iov_len = sizeof(resp);
    
    int ret = recvmsg(sock_fd, &msg, 0);
    if (ret < 0) {
        snprintf(err_str, max_len, "Failed to receive netlink response: %s", strerror(errno));
        close(sock_fd);
        return -1;
    }

    // Check for errors
    struct nlmsghdr* resp_hdr = (struct nlmsghdr*)resp;
    if (resp_hdr->nlmsg_type == NLMSG_ERROR) {
        struct nlmsgerr* err = (struct nlmsgerr*)NLMSG_DATA(resp_hdr);
        if (err->error) {
            snprintf(err_str, max_len, "Netlink error: %s (%d)", strerror(-err->error), -err->error);
            close(sock_fd);
            return -1;
        }
    }
    
    close(sock_fd);
    return 0;
}

// Add IP address to interface using netlink (replaces ip addr add)
static int add_addr_(const char *if_name, const char *addr_str, char *err_str, size_t max_len) {
    // Parse IP address string
    char ip_str[40] = {0};
    int prefix_len = 24;  // Default prefix length
    
    if (sscanf(addr_str, "%39[^/]/%d", ip_str, &prefix_len) < 1) {
        snprintf(err_str, max_len, "Invalid IP address format: %s", addr_str);
        return -1;
    }
    
    // Get interface index
    unsigned int if_idx = if_nametoindex(if_name);
    if (if_idx == 0) {
        snprintf(err_str, max_len, "Interface not found: %s", if_name);
        return -1;
    }
    
    // Create netlink socket
    int sock_fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE);
    if (sock_fd < 0) {
        snprintf(err_str, max_len, "Failed to open netlink socket: %s", strerror(errno));
        return -1;
    }
    
    // Bind the socket
    struct sockaddr_nl sa = {
        .nl_family = AF_NETLINK,
        .nl_pid = 0,  // Let kernel assign a unique PID
        .nl_groups = 0
    };
    
    if (bind(sock_fd, (struct sockaddr*)&sa, sizeof(sa)) < 0) {
        snprintf(err_str, max_len, "Failed to bind netlink socket: %s", strerror(errno));
        close(sock_fd);
        return -1;
    }
    
    // Get our port number
    socklen_t addr_len = sizeof(sa);
    if (getsockname(sock_fd, (struct sockaddr*)&sa, &addr_len) < 0) {
        snprintf(err_str, max_len, "Failed to get socket name: %s", strerror(errno));
        close(sock_fd);
        return -1;
    }
    
    // Generate sequence number
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    uint32_t seq = ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
    
    // Prepare netlink message for adding IP address
    uint8_t buf[512] = {0};
    struct nlmsghdr* nl_hdr = (struct nlmsghdr*)buf;
    nl_hdr->nlmsg_len = NLMSG_LENGTH(sizeof(struct ifaddrmsg));
    nl_hdr->nlmsg_type = RTM_NEWADDR;  // Add address
    nl_hdr->nlmsg_flags = NLM_F_REQUEST | NLM_F_CREATE | NLM_F_ACK | NLM_F_EXCL;
    nl_hdr->nlmsg_seq = seq;
    nl_hdr->nlmsg_pid = sa.nl_pid;
    
    // Address message
    struct ifaddrmsg* addr_msg = NLMSG_DATA(nl_hdr);
    memset(addr_msg, 0, sizeof(struct ifaddrmsg));
    addr_msg->ifa_family = AF_INET;  // IPv4
    addr_msg->ifa_prefixlen = prefix_len;
    addr_msg->ifa_flags = IFA_F_PERMANENT;
    addr_msg->ifa_scope = RT_SCOPE_UNIVERSE;
    addr_msg->ifa_index = if_idx;
    
    // Add IP address attribute
    struct rtattr* rta = (struct rtattr*)((char*)nl_hdr + NLMSG_ALIGN(nl_hdr->nlmsg_len));
    rta->rta_type = IFA_LOCAL;
    rta->rta_len = RTA_LENGTH(4);  // IPv4 address length
    
    struct in_addr ip_addr;
    if (inet_pton(AF_INET, ip_str, &ip_addr) <= 0) {
        snprintf(err_str, max_len, "Invalid IP address: %s", ip_str);
        close(sock_fd);
        return -1;
    }
    
    memcpy(RTA_DATA(rta), &ip_addr, sizeof(ip_addr));
    nl_hdr->nlmsg_len = NLMSG_ALIGN(nl_hdr->nlmsg_len) + RTA_ALIGN(rta->rta_len);
    
    // Add address attribute (same as local for IPv4)
    rta = (struct rtattr*)((char*)nl_hdr + NLMSG_ALIGN(nl_hdr->nlmsg_len));
    rta->rta_type = IFA_ADDRESS;
    rta->rta_len = RTA_LENGTH(4);
    memcpy(RTA_DATA(rta), &ip_addr, sizeof(ip_addr));
    nl_hdr->nlmsg_len = NLMSG_ALIGN(nl_hdr->nlmsg_len) + RTA_ALIGN(rta->rta_len);
    
    // Send message
    struct sockaddr_nl dst = {
        .nl_family = AF_NETLINK,
        .nl_pid = 0,  // To kernel
        .nl_groups = 0
    };
    
    struct iovec iov = {
        .iov_base = nl_hdr,
        .iov_len = nl_hdr->nlmsg_len
    };
    
    struct msghdr msg = {
        .msg_name = &dst,
        .msg_namelen = sizeof(dst),
        .msg_iov = &iov,
        .msg_iovlen = 1
    };
    
    if (sendmsg(sock_fd, &msg, 0) < 0) {
        snprintf(err_str, max_len, "Failed to send netlink message: %s", strerror(errno));
        close(sock_fd);
        return -1;
    }
    
    // Receive response
    char resp[512];
    iov.iov_base = resp;
    iov.iov_len = sizeof(resp);
    
    int ret = recvmsg(sock_fd, &msg, 0);
    if (ret < 0) {
        snprintf(err_str, max_len, "Failed to receive netlink response: %s", strerror(errno));
        close(sock_fd);
        return -1;
    }
    
    // Check for errors
    struct nlmsghdr* resp_hdr = (struct nlmsghdr*)resp;
    if (resp_hdr->nlmsg_type == NLMSG_ERROR) {
        struct nlmsgerr* err = (struct nlmsgerr*)NLMSG_DATA(resp_hdr);
        if (err->error) {
            snprintf(err_str, max_len, "Netlink error: %s (%d)", strerror(-err->error), -err->error);
            close(sock_fd);
            return -1;
        }
    }
    
    close(sock_fd);
    return 0;
}

// Set interface up using netlink (replaces ip link set up)
static int set_link_up_(const char *if_name, char *err_str, size_t max_len) {
    // Get interface index
    unsigned int if_idx = if_nametoindex(if_name);
    if (if_idx == 0) {
        snprintf(err_str, max_len, "Interface not found: %s", if_name);
        return -1;
    }
    
    // Create netlink socket
    int sock_fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE);
    if (sock_fd < 0) {
        snprintf(err_str, max_len, "Failed to open netlink socket: %s", strerror(errno));
        return -1;
    }
    
    // Bind the socket
    struct sockaddr_nl sa = {
        .nl_family = AF_NETLINK,
        .nl_pid = 0,  // Let kernel assign a unique PID
        .nl_groups = 0
    };
    
    if (bind(sock_fd, (struct sockaddr*)&sa, sizeof(sa)) < 0) {
        snprintf(err_str, max_len, "Failed to bind netlink socket: %s", strerror(errno));
        close(sock_fd);
        return -1;
    }
    
    // Get our port number
    socklen_t addr_len = sizeof(sa);
    if (getsockname(sock_fd, (struct sockaddr*)&sa, &addr_len) < 0) {
        snprintf(err_str, max_len, "Failed to get socket name: %s", strerror(errno));
        close(sock_fd);
        return -1;
    }
    
    // Generate sequence number
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    uint32_t seq = ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
    
    // Prepare netlink message for setting interface up
    uint8_t buf[512] = {0};
    struct nlmsghdr* nl_hdr = (struct nlmsghdr*)buf;
    nl_hdr->nlmsg_len = NLMSG_LENGTH(sizeof(struct ifinfomsg));
    nl_hdr->nlmsg_type = RTM_NEWLINK;  // Modify link
    nl_hdr->nlmsg_flags = NLM_F_REQUEST | NLM_F_ACK;
    nl_hdr->nlmsg_seq = seq;
    nl_hdr->nlmsg_pid = sa.nl_pid;
    
    // Interface info message
    struct ifinfomsg* if_msg = NLMSG_DATA(nl_hdr);
    memset(if_msg, 0, sizeof(struct ifinfomsg));
    if_msg->ifi_family = AF_UNSPEC;
    if_msg->ifi_index = if_idx;
    if_msg->ifi_change = IFF_UP;  // Change UP flag
    if_msg->ifi_flags = IFF_UP;   // Set UP flag
    
    // Send message
    struct sockaddr_nl dst = {
        .nl_family = AF_NETLINK,
        .nl_pid = 0,  // To kernel
        .nl_groups = 0
    };
    
    struct iovec iov = {
        .iov_base = nl_hdr,
        .iov_len = nl_hdr->nlmsg_len
    };
    
    struct msghdr msg = {
        .msg_name = &dst,
        .msg_namelen = sizeof(dst),
        .msg_iov = &iov,
        .msg_iovlen = 1
    };
    
    if (sendmsg(sock_fd, &msg, 0) < 0) {
        snprintf(err_str, max_len, "Failed to send netlink message: %s", strerror(errno));
        close(sock_fd);
        return -1;
    }
    
    // Receive response
    char resp[512];
    iov.iov_base = resp;
    iov.iov_len = sizeof(resp);
    
    int ret = recvmsg(sock_fd, &msg, 0);
    if (ret < 0) {
        snprintf(err_str, max_len, "Failed to receive netlink response: %s", strerror(errno));
        close(sock_fd);
        return -1;
    }
    
    // Check for errors
    struct nlmsghdr* resp_hdr = (struct nlmsghdr*)resp;
    if (resp_hdr->nlmsg_type == NLMSG_ERROR) {
        struct nlmsgerr* err = (struct nlmsgerr*)NLMSG_DATA(resp_hdr);
        if (err->error) {
            snprintf(err_str, max_len, "Netlink error: %s (%d)", strerror(-err->error), -err->error);
            close(sock_fd);
            return -1;
        }
    }
    
    close(sock_fd);
    return 0;
}

// Initialize interface (add addr, setup tc, set link up)
static int init_if_(const char *if_name, const char *addr_str, 
                   uint32_t delay_ms, uint32_t loss_percent, 
                   const char *rate_str, char *err_str, size_t max_len) {
    // 1. Add IP address
    if (add_addr_(if_name, addr_str, err_str, max_len) != 0) {
        return -1;
    }
    
    // 2. Add traffic control qdisc
    // Create netlink socket
    int sock_fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE);
    if (sock_fd < 0) {
        snprintf(err_str, max_len, "Failed to open netlink socket: %s", strerror(errno));
        return -1;
    }
    
    // Bind the socket
    struct sockaddr_nl sa = {
        .nl_family = AF_NETLINK,
        .nl_pid = 0,
        .nl_groups = 0
    };
    
    if (bind(sock_fd, (struct sockaddr*)&sa, sizeof(sa)) < 0) {
        snprintf(err_str, max_len, "Failed to bind netlink socket: %s", strerror(errno));
        close(sock_fd);
        return -1;
    }
    
    // Get interface index
    unsigned int if_idx = if_nametoindex(if_name);
    if (if_idx == 0) {
        snprintf(err_str, max_len, "Interface not found: %s", if_name);
        close(sock_fd);
        return -1;
    }
    
    // Parse rate string (similar to update_netem_)
    double rate_value = 0.0;
    char rate_unit[16] = {0};
    uint64_t rate_bps = 0;

    if (sscanf(rate_str, "%lf%15s", &rate_value, rate_unit) == 2) {
        if (strcmp(rate_unit, "Gbit") == 0) {
            rate_bps = (uint64_t)(rate_value * 1000000000 / 8);
        } else if (strcmp(rate_unit, "Mbit") == 0) {
            rate_bps = (uint64_t)(rate_value * 1000000 / 8);
        } else if (strcmp(rate_unit, "Kbit") == 0) {
            rate_bps = (uint64_t)(rate_value * 1000 / 8);
        } else {
            rate_bps = (uint64_t)rate_value;
        }
    } else {
        if (sscanf(rate_str, "%lf", &rate_value) == 1) {
            rate_bps = (uint64_t)rate_value * 1000000000 / 8;
        }
    }
    
    // Get our port number
    socklen_t addr_len = sizeof(sa);
    if (getsockname(sock_fd, (struct sockaddr*)&sa, &addr_len) < 0) {
        snprintf(err_str, max_len, "Failed to get socket name: %s", strerror(errno));
        close(sock_fd);
        return -1;
    }
    
    // Generate sequence number
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    uint32_t seq = ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
    
    // Prepare netlink message for adding qdisc
    uint8_t buf[1024] = {0};
    struct nlmsghdr* nl_hdr = (struct nlmsghdr*)buf;
    nl_hdr->nlmsg_len = NLMSG_LENGTH(sizeof(struct tcmsg));
    nl_hdr->nlmsg_type = RTM_NEWQDISC;
    nl_hdr->nlmsg_flags = NLM_F_REQUEST | NLM_F_ACK | NLM_F_CREATE | NLM_F_EXCL;
    nl_hdr->nlmsg_seq = seq;
    nl_hdr->nlmsg_pid = sa.nl_pid;
    
    // TC message structure
    struct tcmsg* tc_msg = NLMSG_DATA(nl_hdr);
    tc_msg->tcm_family = AF_UNSPEC;
    tc_msg->tcm__pad1 = tc_msg->tcm__pad2 = 0;
    tc_msg->tcm_ifindex = if_idx;
    tc_msg->tcm_handle = 0;
    tc_msg->tcm_parent = TC_H_ROOT;
    tc_msg->tcm_info = 0;
    
    // Add qdisc kind attribute (netem)
    struct rtattr* rta = (struct rtattr*)((char*)nl_hdr + NLMSG_ALIGN(nl_hdr->nlmsg_len));
    rta->rta_type = TCA_KIND;
    rta->rta_len = RTA_LENGTH(strlen("netem") + 1);
    memcpy(RTA_DATA(rta), "netem", strlen("netem") + 1);
    nl_hdr->nlmsg_len = NLMSG_ALIGN(nl_hdr->nlmsg_len) + RTA_ALIGN(rta->rta_len);
    
    // Options
    struct rtattr* opts = (struct rtattr*)((char*)nl_hdr + NLMSG_ALIGN(nl_hdr->nlmsg_len));
    opts->rta_type = TCA_OPTIONS;
    opts->rta_len = RTA_LENGTH(sizeof(struct tc_netem_qopt));
    
    // Set netem parameters
    struct tc_netem_qopt* qopt = (struct tc_netem_qopt*)RTA_DATA(opts);
    memset(qopt, 0, sizeof(*qopt));
    qopt->limit = 1000;
    qopt->latency = delay_ms * 1000;  // Convert ms to us
    qopt->loss = loss_percent * 10000 / 100;  // Convert % to ppm
    
    // Update total length
    nl_hdr->nlmsg_len = NLMSG_ALIGN(nl_hdr->nlmsg_len) + RTA_ALIGN(opts->rta_len);
    
    // If we have a rate specified, add rate information
    if (rate_bps > 0) {
        struct rtattr* rate_attr = (struct rtattr*)((char*)opts + RTA_ALIGN(opts->rta_len));
        rate_attr->rta_type = TCA_NETEM_RATE;
        rate_attr->rta_len = RTA_LENGTH(sizeof(struct tc_netem_rate));
        
        struct tc_netem_rate* rate = (struct tc_netem_rate*)RTA_DATA(rate_attr);
        memset(rate, 0, sizeof(*rate));
        rate->rate = (rate_bps < (1ULL << 32)) ? rate_bps : ~0U;
        
        // Update options length
        opts->rta_len += RTA_ALIGN(rate_attr->rta_len);
        nl_hdr->nlmsg_len += RTA_ALIGN(rate_attr->rta_len);
        
        // Add 64-bit value if needed
        if (rate_bps >= (1ULL << 32)) {
            struct rtattr* rate64 = (struct rtattr*)((char*)rate_attr + RTA_ALIGN(rate_attr->rta_len));
            rate64->rta_type = TCA_NETEM_RATE64;
            rate64->rta_len = RTA_LENGTH(sizeof(uint64_t));
            *(uint64_t*)RTA_DATA(rate64) = rate_bps;
            
            // Update lengths
            opts->rta_len += RTA_ALIGN(rate64->rta_len);
            nl_hdr->nlmsg_len += RTA_ALIGN(rate64->rta_len);
        }
    }
    
    // Send message
    struct sockaddr_nl dst = {
        .nl_family = AF_NETLINK,
        .nl_pid = 0,
        .nl_groups = 0
    };
    
    struct iovec iov = {
        .iov_base = nl_hdr,
        .iov_len = nl_hdr->nlmsg_len
    };
    
    struct msghdr msg = {
        .msg_name = &dst,
        .msg_namelen = sizeof(dst),
        .msg_iov = &iov,
        .msg_iovlen = 1
    };
    
    if (sendmsg(sock_fd, &msg, 0) < 0) {
        snprintf(err_str, max_len, "Failed to send netlink message: %s", strerror(errno));
        close(sock_fd);
        return -1;
    }
    
    // Receive response
    char resp[1024];
    iov.iov_base = resp;
    iov.iov_len = sizeof(resp);
    
    int ret = recvmsg(sock_fd, &msg, 0);
    if (ret < 0) {
        snprintf(err_str, max_len, "Failed to receive netlink response: %s", strerror(errno));
        close(sock_fd);
        return -1;
    }
    
    // Check for errors
    struct nlmsghdr* resp_hdr = (struct nlmsghdr*)resp;
    if (resp_hdr->nlmsg_type == NLMSG_ERROR) {
        struct nlmsgerr* err = (struct nlmsgerr*)NLMSG_DATA(resp_hdr);
        if (err->error) {
            snprintf(err_str, max_len, "TC error: %s (%d)", strerror(-err->error), -err->error);
            close(sock_fd);
            return -1;
        }
    }
    
    close(sock_fd);
    
    // 3. Set interface up
    if (set_link_up_(if_name, err_str, max_len) != 0) {
        return -1;
    }
    
    return 0;
}

// Python function to update network interface parameters
static PyObject* pynetlink_update_if(PyObject* self, PyObject* args) {
    const char *if_name;
    const char *delay_str;
    const char *rate_str;
    const char *loss_str;
    char err[256];
    
    // Parse Python arguments
    if (!PyArg_ParseTuple(args, "ssss", &if_name, &delay_str, &rate_str, &loss_str)) {
        return NULL;
    }
    
    // Convert delay string to integer (ms)
    int delay_ms = atoi(delay_str);
    
    // Convert loss string to integer (%)
    int loss_percent = atoi(loss_str);
    
    // Update netem qdisc
    int result = update_netem_(if_name, delay_ms, loss_percent, rate_str, err, sizeof(err));
    
    if (result < 0) {
        PyErr_SetString(PyExc_RuntimeError, err);
        return NULL;
    }
    
    Py_RETURN_NONE;
}

// Python function to delete a network interface
static PyObject* pynetlink_del_link(PyObject* self, PyObject* args) {
    const char *if_name;
    char err[256];
    
    if (!PyArg_ParseTuple(args, "s", &if_name)) {
        return NULL;
    }
    
    int result = del_link_(if_name, err, sizeof(err));
    if (result < 0) {
        PyErr_SetString(PyExc_RuntimeError, err);
        return NULL;
    }
    
    Py_RETURN_NONE;
}

// Python function to initialize an interface (addr, tc, up)
static PyObject* pynetlink_init_if(PyObject* self, PyObject* args) {
    const char *if_name;
    const char *addr_str;
    const char *delay_str;
    const char *rate_str;
    const char *loss_str;
    char err[256];
    
    if (!PyArg_ParseTuple(args, "sssss", &if_name, &addr_str, &delay_str, &rate_str, &loss_str)) {
        return NULL;
    }
    
    // Convert strings to numeric values
    int delay_ms = atoi(delay_str);
    int loss_percent = atoi(loss_str);
    
    int result = init_if_(if_name, addr_str, delay_ms, loss_percent, rate_str, err, sizeof(err));
    if (result < 0) {
        PyErr_SetString(PyExc_RuntimeError, err);
        return NULL;
    }
    
    Py_RETURN_NONE;
}

// Define module methods
static PyMethodDef PyNetlinkMethods[] = {
    {"update_if", pynetlink_update_if, METH_VARARGS, "Update network interface parameters using netlink"},
    {"del_link", pynetlink_del_link, METH_VARARGS, "Delete a network interface using netlink"},
    {"init_if", pynetlink_init_if, METH_VARARGS, "Initialize an interface (add addr, setup tc, set up)"},
    {NULL, NULL, 0, NULL}
};

// Define module
static struct PyModuleDef pynetlink_module = {
    PyModuleDef_HEAD_INIT,
    "pynetlink",
    "Python extension for efficient network interface updates using netlink",
    -1,
    PyNetlinkMethods
};

// Initialize module
PyMODINIT_FUNC PyInit_pynetlink(void) {
    return PyModule_Create(&pynetlink_module);
}

